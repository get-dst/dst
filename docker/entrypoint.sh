#!/bin/sh
# dst container entrypoint: optionally wait for the DB + migrate, then serve.
# Migrate-on-start (the default) is idempotent and advisory-locked — right for
# compose and single-instance deploys. Orchestrated deploys (helm's hook Job, a
# Cloud Run release step) set DST_MIGRATE_ON_START=false and run
# `dst migrate` once per release instead. DATABASE_ADMIN_URL is still a
# runtime dependency either way — admin-token auth, the scheduler, and OAuth
# run on the admin engine.
set -e

if [ "${DST_MIGRATE_ON_START:-true}" = "true" ]; then
    echo "dst: waiting for database…"
    /app/.venv/bin/python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine

url = os.environ.get("DATABASE_ADMIN_URL")
if not url:
    sys.exit("DATABASE_ADMIN_URL is not set (or set DST_MIGRATE_ON_START=false)")
for _ in range(60):
    try:
        create_engine(url).connect().close()
        sys.exit(0)
    except Exception:
        time.sleep(2)
sys.exit("database never became ready")
PY

    echo "dst: running migrations…"
    /app/.venv/bin/dst migrate
fi

# --forwarded-allow-ips: uvicorn only honors X-Forwarded-* from trusted peers,
# and its default (127.0.0.1) never matches a container network — behind any
# proxy/ingress the app then sees http:// and the session cookie loses Secure.
# In-container the peer IS your proxy, so trust-all is the sane default;
# narrow it with FORWARDED_ALLOW_IPS when the container port is reachable
# from anything that isn't the proxy.
exec /app/.venv/bin/uvicorn services.app:app --host 0.0.0.0 --port "${PORT:-8000}" \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
