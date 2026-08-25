"""Per-caller rate limiting (limiter unit tests + endpoint 429)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import AccessRule
from services.db.session import org_session
from services.governance import credentials, ratelimit
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def test_limiter_allows_then_blocks() -> None:
    ratelimit.reset()
    key = "o:lens:caller"
    t = 1000.0
    assert ratelimit.check(key, 2, now=t) is True
    assert ratelimit.check(key, 2, now=t) is True
    assert ratelimit.check(key, 2, now=t) is False  # third in-window denied


def test_limiter_window_slides() -> None:
    ratelimit.reset()
    key = "o:lens:caller"
    assert ratelimit.check(key, 1, now=0.0) is True
    assert ratelimit.check(key, 1, now=1.0) is False
    assert ratelimit.check(key, 1, now=61.0) is True  # old hit expired


def test_limiter_zero_means_unlimited() -> None:
    ratelimit.reset()
    for _ in range(100):
        assert ratelimit.check("k", 0) is True


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@needs_db
def test_endpoint_returns_429_over_limit() -> None:
    """A non-admin caller over per_caller_rpm gets 429 before any LLM work."""
    ratelimit.reset()
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('RLTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    try:
        with org_session(org) as s:
            cid = credentials.create_caller(s, "agent-1")
            caller_key = credentials.issue_key(s, cid)
            bundle = jaffle_customer_value_bundle()
            bundle.config.rate_limit.per_caller_rpm = 2
            bundle.config.access.allow = [AccessRule(caller="agent-1")]
            store.create_lens(s, bundle)
            store.publish(s, "customer_value")

        h = {"Authorization": f"Bearer {caller_key}"}
        body = {"q": "how many repeat customers?"}
        # The first two calls may 200, 503 (no key), or 500 (live upstream having a
        # bad day) — anything but 429. Only the third call's 429 is under test.
        lenient = TestClient(app, raise_server_exceptions=False)
        codes = [
            lenient.post("/v1/lenses/customer_value/query", json=body, headers=h).status_code
            for _ in range(3)
        ]
        assert codes[0] != 429
        assert codes[1] != 429
        assert codes[2] == 429
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM audit_log WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM api_key WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM caller WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
