"""The `dst demo` command."""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _demo(args: argparse.Namespace) -> int:
    from services.config import resolve_env_ref, settings
    from services.contracts.lens_config import AccessRule
    from services.db.session import org_session
    from services.lenses import connection_store, store
    from services.lenses.connections import build_connector
    from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
    from services.semantic import store as semantic_store

    # The warehouse the demo lens reads: the bundled file by default, or any
    # DuckDB path — `md:<database>` is MotherDuck, token via --secret-env. A
    # public deployment sets the timeout: the file is no longer only yours.
    path = getattr(args, "path", None) or settings.duckdb_jaffle_path
    config: dict[str, object] = {"path": path}
    timeout = getattr(args, "statement_timeout_ms", None)
    if timeout:
        config["statement_timeout_ms"] = int(timeout)
    secret: str | None = None
    secret_env = getattr(args, "secret_env", None)
    if secret_env:
        secret = resolve_env_ref(secret_env)
        if not secret:
            print(f"error: {secret_env} is not set (the warehouse token)", file=sys.stderr)
            return 2
    # Probe before anything lands: a dead path or token is a refusal here, not a
    # published lens that fails on its first question.
    try:
        build_connector("duckdb", config, secret).execute("SELECT 1")
    except Exception as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    with org_session(args.org_id) as session:
        existing = connection_store.get_connection(session, "jaffle")
        if existing is None:
            connection_store.create_connection(session, "jaffle", "duckdb", config, secret)
        elif dict(existing.config or {}) != config or secret is not None:
            connection_store.update_connection(session, "jaffle", config, secret)
        # The demo dogfoods the shared layer: seed its assets so the published
        # bundle's compile provenance matches the store (never spuriously stale).
        entities, definitions, relationships = jaffle_shared_assets()
        for e in entities:
            semantic_store.upsert_asset(session, "entity", e.name, e.model_dump(mode="json"))
        for d in definitions:
            semantic_store.upsert_asset(session, "definition", d.term, d.model_dump(mode="json"))
        for r in relationships:
            semantic_store.upsert_asset(session, "relationship", r.name, r.model_dump(mode="json"))
        bundle = jaffle_customer_value_bundle()
        # Who may ask, and how much: the bundled lens grants nobody by default
        # (admins only). A public demo grants `group: demo` and bounds a day.
        groups = list(getattr(args, "allow_group", None) or [])
        if groups:
            bundle.config.access.allow = [AccessRule(group=g) for g in groups]
        rpd = getattr(args, "per_caller_rpd", None)
        if rpd:
            bundle.config.rate_limit.per_caller_rpd = int(rpd)
        if store.lens_exists(session, bundle.config.name):
            store.update_draft(session, bundle.config.name, bundle)
        else:
            store.create_lens(session, bundle)
        store.publish(session, bundle.config.name)
        session.commit()
    grant = f", allow {'/'.join(groups)}" if groups else ""
    quota = f", {rpd}/day per caller" if rpd else ""
    print(f"demo lens '{bundle.config.name}' published (duckdb {path}{grant}{quota})")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("demo", help="publish the bundled duckdb demo lens into an org")
    p.add_argument("--org-id", required=True, help="org UUID from `dst bootstrap`")
    p.add_argument(
        "--path",
        help="the DuckDB the lens reads: a file, or md:<database> for MotherDuck "
        "(default: the bundled jaffle fixture)",
    )
    p.add_argument(
        "--secret-env",
        help="env var holding the warehouse token (MotherDuck), read from the process "
        "env or ./.env — the value itself is never a flag",
    )
    p.add_argument(
        "--statement-timeout-ms",
        type=int,
        help="cancel a query after this long (unset = unbounded; set it on anything "
        "reachable from outside)",
    )
    p.add_argument(
        "--allow-group",
        action="append",
        metavar="GROUP",
        help="grant the lens to a caller group (repeatable; e.g. demo, or everyone). "
        "Default: admins only",
    )
    p.add_argument(
        "--per-caller-rpd",
        type=int,
        help="daily answers per caller on the lens (0 = unlimited, the default)",
    )
    p.set_defaults(fn=_demo)
