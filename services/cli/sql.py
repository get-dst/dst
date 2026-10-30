"""The `dst sql` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli._common import (
    CLI_AGENT,
    _client,
    _declared_connection,
    _detail,
    _table,
)


def _sql(args: argparse.Namespace) -> int:
    """Read-only SQL against a connection — the governed version of opening DuckDB.

    `introspect` says what the columns ARE; it cannot say what they contain
    together, and deciding a business rule ("is a refund a negative amount, or a
    row with status='refunded'?") takes five actual rows. Authors did that
    anyway, outside the guard and outside the audit log. Same read, run through
    sql_guard, row-capped, and logged to request_log.

    Complements `introspect --profile` rather than repeating it: per-column enum
    values, null rates and ranges come from the profile in one pass; this is for
    rows, cross-column facts, and joins a profile cannot see."""
    import httpx

    # An EXPLICIT --key is always sent, even with --connection, so the refusal comes
    # from the server ("probing a whole connection needs an admin token — pass `lens`")
    # instead of a generic "no admin token" that never mentions the key you passed.
    # The DST_API_KEY fallback stays scoped to --lens: a project .env holding one
    # must not silently downgrade an admin's connection probe.
    url, headers = _client(args, caller_key_ok=bool(args.lens or args.key))
    scope = {"connection": args.connection} if args.connection else {"lens": args.lens}
    r = httpx.post(
        f"{url}/v1/sql",
        headers={**headers, "X-Dst-Agent": CLI_AGENT},
        json={"sql": args.sql, "limit": args.limit, **scope},
        timeout=args.timeout,
    )
    if r.status_code == 404 and args.connection and _declared_connection(args)[0]:
        path = Path(getattr(args, "dir", ".") or ".") / "dst.yaml"
        print(
            f"error: connection '{args.connection}' is declared in {path} but no server has "
            "it yet — run `dst apply`. (`dst introspect` reads the file directly; "
            "this command runs on the server, which is what puts the probe in the audit log.)",
            file=sys.stderr,
        )
        return 1
    if r.status_code >= 400:
        detail = _detail(r)
        print(f"error: {detail}", file=sys.stderr)
        # The refusal named what is disallowed and not what to reach for, and the
        # measured next move was a raw DuckDB client — the one thing this verb
        # exists to make unnecessary. A non-SELECT here is nearly always "what
        # tables/columns are there", which is a different verb, not a workaround.
        if "only SELECT queries are allowed" in detail:
            conn = args.connection or "<name>"
            print(
                f"  to list tables and columns, run `dst introspect --connection {conn}`"
                " (add --profile for row counts, enum values and null rates)",
                file=sys.stderr,
            )
        return 1
    d = r.json()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    if d["columns"]:
        print(_table(d["columns"], d["rows"]))
    tail = f"({d['row_count']} rows"
    print(tail + "; capped — raise --limit for more)" if d["truncated"] else tail + ")")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "sql",
        help="run read-only SQL against a connection and see the rows — guarded, "
        "row-capped, and logged. For per-column enums/null rates/ranges reach for "
        "`introspect --profile` instead; this is for rows and cross-column facts",
    )
    p.add_argument("sql", help="a single SELECT statement")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--connection", help="probe a whole connection (needs an admin token)")
    scope.add_argument(
        "--lens", help="probe within a lens's allow-list (works with a dst_ caller key)"
    )
    p.add_argument("--limit", type=int, default=20, help="rows to return (default 20, max 500)")
    p.add_argument("--json", action="store_true", help="columns + rows as an object")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--key", help="a dst_ caller key, for --lens (default: DST_API_KEY)")
    p.set_defaults(fn=_sql)
