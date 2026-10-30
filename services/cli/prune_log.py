"""The `dst prune-log` command."""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any


def _prune_log(args: argparse.Namespace) -> int:
    """Delete request_log rows older than --keep-days, per org (the log is FORCE
    RLS: the GUC is set per org so the sweep is honest on managed Postgres,
    where the admin role is not a superuser). The rows carry the questions
    people asked; a public deployment promises them a bounded life."""
    from sqlalchemy import text

    from services.db.session import admin_engine

    if args.keep_days < 1:
        print("error: --keep-days must be at least 1", file=sys.stderr)
        return 2
    with admin_engine.connect() as c:
        if args.org_id:
            rows = c.execute(
                text("SELECT id, name FROM org WHERE id = :o"), {"o": args.org_id}
            ).all()
        else:
            rows = c.execute(text("SELECT id, name FROM org ORDER BY created_at")).all()
    if not rows:
        print("no org matched", file=sys.stderr)
        return 2
    total = 0
    for oid, name in rows:
        with admin_engine.begin() as c:
            c.execute(text(f"SET LOCAL app.current_org = '{uuid.UUID(str(oid))}'"))
            res = c.execute(
                text(
                    "DELETE FROM request_log WHERE org_id = :o "
                    "AND created_at < now() - make_interval(days => :d)"
                ),
                {"o": oid, "d": int(args.keep_days)},
            )
            # The per-call usage rows age with the requests they belong to.
            c.execute(
                text(
                    "DELETE FROM ai_call WHERE org_id = :o "
                    "AND created_at < now() - make_interval(days => :d)"
                ),
                {"o": oid, "d": int(args.keep_days)},
            )
        n = int(res.rowcount)
        total += n
        print(f"{name}: {n} row(s) older than {args.keep_days} day(s) deleted")
    print(f"pruned {total} row(s) across {len(rows)} org(s)")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "prune-log",
        help="delete request_log rows older than N days (every org, or --org-id)",
    )
    p.add_argument("--keep-days", type=int, required=True, help="rows younger than this stay")
    p.add_argument("--org-id", help="one org (default: every org in the database)")
    p.set_defaults(fn=_prune_log)
