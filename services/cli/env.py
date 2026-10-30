"""The `dst env` command."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

from services.cli import style
from services.cli._common import (
    _envs_file,
    _envs_read,
)


def _env(args: argparse.Namespace) -> int:
    """Disposable environments. An environment IS an org on this server — these
    verbs manage orgs, they do not introduce a new resource."""
    from sqlalchemy import text

    from services.db.session import admin_engine

    if args.action != "ls" and not args.name:
        print(f"error: `dst env {args.action}` needs a name", file=sys.stderr)
        return 1

    # Managed-PG admin roles lack BYPASSRLS: FORCE-RLS tables are invisible to
    # them until the org GUC is set, so every per-org count below sets it first
    # (same class as tests/test_rls_managed_pg.py).
    def _counts(c: Any, org_id: Any) -> tuple[int, int, int]:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
        return c.execute(  # type: ignore[no-any-return]
            text(
                "SELECT (SELECT count(*) FROM lens WHERE org_id = :o),"
                " (SELECT count(*) FROM caller WHERE org_id = :o),"
                " (SELECT count(*) FROM request_log WHERE org_id = :o)"
            ),
            {"o": org_id},
        ).one()

    if args.action == "new":
        from services.auth import tokens

        raw = tokens.new_admin_token()
        with admin_engine.begin() as c:
            org_id = c.execute(
                text("SELECT id FROM org WHERE name = :n ORDER BY created_at LIMIT 1"),
                {"n": args.name},
            ).scalar()
            created = org_id is None
            if org_id is None:
                org_id = c.execute(
                    text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": args.name}
                ).scalar_one()
            c.execute(
                text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, :l)"),
                {"o": org_id, "h": hashlib.sha256(raw.encode()).hexdigest(), "l": "env"},
            )
        print(
            f"env: {args.name} ({org_id}) — "
            + ("created" if created else "reused (fresh token minted; earlier tokens stay valid)")
        )
        # Unlike bootstrap this never writes .env: a sandbox must not steal the
        # project's identity. The token lands in .dst/envs.json instead, so
        # local commands reach the env by NAME (--env) and no human handles
        # the secret; the export line stays for CI and remote shells.
        _envs_write({**_envs_read(), args.name: raw})
        print(f"recorded in {_envs_file()} — local commands can use --env {args.name}")
        print(f"admin token (shown once): {raw}")
        print("wire a remote shell or a CI job:")
        print(f"  export DST_ADMIN_TOKEN={raw}")
        return 0

    if args.action == "ls":
        with admin_engine.begin() as c:
            orgs = c.execute(text("SELECT id, name, created_at FROM org ORDER BY created_at")).all()
            rows = [(name, oid, created, *_counts(c, oid)) for oid, name, created in orgs]
        if not rows:
            print("no environments on this server")
            return 0
        print(f"{'NAME':<24} {'LENSES':>6} {'CALLERS':>7} {'TRACES':>7}  CREATED")
        for name, oid, created, lenses, callers, traces in rows:
            print(
                f"{name:<24} {lenses:>6} {callers:>7} {traces:>7}"
                f"  {created:%Y-%m-%d} {style.dim(str(oid))}"
            )
        return 0

    # rm
    if not args.yes:
        print("error: `dst env rm` is irreversible — re-run with --yes", file=sys.stderr)
        return 1
    with admin_engine.begin() as c:
        ids = (
            c.execute(text("SELECT id FROM org WHERE name = :n"), {"n": args.name}).scalars().all()
        )
        if not ids:
            print(f"error: no environment named {args.name!r}", file=sys.stderr)
            return 1
        if len(ids) > 1:
            print(
                f"error: {len(ids)} orgs named {args.name!r} — refusing an ambiguous delete",
                file=sys.stderr,
            )
            return 1
        lenses, callers, traces = _counts(c, ids[0])
        # Every org FK is ON DELETE CASCADE (pinned by tests/test_env_cli.py),
        # and referential actions bypass RLS — one delete empties the
        # environment even where the admin role lacks BYPASSRLS.
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": ids[0]})
    known = _envs_read()
    if args.name in known:
        _envs_write({k: v for k, v in known.items() if k != args.name})
    print(f"env {args.name} removed — {lenses} lenses, {callers} callers, {traces} traces gone")
    return 0


def _envs_write(mapping: dict[str, str]) -> None:
    path = _envs_file()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")
    path.chmod(0o600)


def register(sub: Any) -> None:
    p = sub.add_parser(
        "env",
        help="disposable environments — an env is an org on this server: "
        "`new <name>` mints an org + admin token (never writes .env), "
        "`ls` shows each env and what it holds, "
        "`rm <name> --yes` deletes the env and everything in it — irreversible",
    )
    p.add_argument("action", choices=["new", "ls", "rm"])
    p.add_argument("name", nargs="?", help="environment (org) name")
    p.add_argument("--yes", action="store_true", help="rm: confirm the irreversible delete")
    p.add_argument(
        "--url",
        help="ignored — env talks to DATABASE_ADMIN_URL directly (no server needed)",
    )
    p.set_defaults(fn=_env)
