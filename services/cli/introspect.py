"""The `dst introspect` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli._common import (
    _client,
    _declared_connection,
    _env_dirs,
    _read_project,
    _undeclared_connection,
)


def _check_joins(connector: object, root: Path) -> int:
    """Measure every declared join's real cardinality against the warehouse.

    The intent compiler now DECIDES on `relationship`: a hop declared many_to_one is
    joined directly, and a lie there multiplies every additive aggregate downstream
    (a fanout can report a true 1,359,168 in deposits as 9,514,176). Nothing else
    checks the declaration, and 8 of 101 foreign keys in BIRD are not one-to-many in
    the data — so the claim needs measuring, not trusting.

    Read-only and outside apply on purpose: this is authoring feedback, and apply's
    warehouse-touching stages are not a place to add work.
    """
    import sqlglot
    from sqlglot import exp

    from services.project.loader import split_semantic
    from services.semantic.cardinality import measure, verdict
    from services.semantic.files import parse_semantic_files

    entities, _definitions, relationships = parse_semantic_files(
        split_semantic(_read_project(root))
    )
    if not entities:
        print(f"error: no semantic/entities/*.yaml under {root}", file=sys.stderr)
        return 1

    def run(sql: str) -> list[tuple[Any, ...]]:
        return list(connector.execute(sql, read_only=True).rows)  # type: ignore[attr-defined]

    worst = 0
    checked = 0
    for rel in relationships.values():
        left, right = entities.get(rel.left), entities.get(rel.right)
        if left is None or right is None:
            missing = rel.left if left is None else rel.right
            print(
                f"[unknown  ] {rel.left} <-> {rel.right}: no such entity '{missing}'",
                file=sys.stderr,
            )
            worst = max(worst, 1)
            continue
        # EVERY column each side matches on: a composite key measured one column
        # at a time reports a fan-out that the full key does not have (a prices
        # table with 2 rows per market_date, but 1 per (ticker, market_date)).
        columns: dict[str, list[str]] = {}
        for c in sqlglot.parse_one(rel.on, read="duckdb").find_all(exp.Column):
            owned = columns.setdefault(c.table or "", [])
            if c.name not in owned:
                owned.append(c.name)
        left_col = columns.get(rel.left, [])
        right_col = columns.get(rel.right, [])
        if not left_col or not right_col:
            print(
                f"[skipped  ] {rel.left} <-> {rel.right}: could not read one column per "
                f"side out of `{rel.on}`",
                file=sys.stderr,
            )
            continue
        checked += 1
        status, message = verdict(
            rel.relationship,
            measure(
                run,
                left_table=left.source.table,
                left_columns=left_col,
                right_table=right.source.table,
                right_columns=right_col,
                left=rel.left,
                right=rel.right,
            ),
        )
        print(f"[{status:9s}] {message}")
        if status in ("wrong", "unsafe"):
            worst = 1
    if not checked:
        print(
            "no relationships to check — declare them in semantic/relationships/*.yaml",
            file=sys.stderr,
        )
    return worst


def _introspect(args: argparse.Namespace) -> int:
    """Schema + profile facts for a connection, agent-legible — the raw material
    for authoring semantic/ files with your own agent.

    Resolves the connection from the project's dst.yaml first (so it works
    before the first apply), and falls back to the server — which is also where
    stored profile facts live, and the only path when the warehouse is reachable
    from the server but not from here.

    ``--profile`` runs the catalog + sampling passes against the warehouse right
    here. Without it the file-first path has no facts to print and says so: every
    scaffolded project declares its connection in dst.yaml, so this branch
    always wins and the stored-profile facts were unreachable from the CLI.

    Empty output is never success: a warehouse that yields no table says so on
    stderr, naming the schemas searched, and exits non-zero (blocker 1 — three
    agents got a blank line and exit 0 and abandoned the path)."""
    import httpx

    from services.config import resolve_env_ref
    from services.contracts.profile import TableProfile
    from services.semantic.introspect import empty_listing_reason, schema_json, serialize_schema

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else None
    type_, config, secret = _declared_connection(args)
    if not type_ and not (
        getattr(args, "token", None) or resolve_env_ref("DST_ADMIN_TOKEN", dirs=_env_dirs(args))
    ):
        # Nothing declares it and there is no server to ask: the missing
        # declaration is the actual problem, not the missing token.
        print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
        return 1
    if type_:
        from services.contracts.protocols import TargetedIntrospect
        from services.lenses.connections import build_connector, config_warnings

        for note in config_warnings(type_, config):
            print(f"warning: {note}", file=sys.stderr)
        try:
            connector = build_connector(type_, config, secret)
            # --tables resolves against the FULL catalog where the connector can
            # (a capped listing poisoned every match on a wide warehouse).
            if tables and isinstance(connector, TargetedIntrospect):
                snapshot = connector.introspect_tables(tables)
            else:
                snapshot = connector.introspect()
        except Exception as exc:  # noqa: BLE001 — a local dead end must not end the command
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            print(
                f"warning: connection '{args.connection}' declared in dst.yaml did not "
                f"introspect from here ({first}) — trying the server",
                file=sys.stderr,
            )
        else:
            reason = empty_listing_reason(args.connection, snapshot, tables)
            if reason is not None:
                print(f"error: {reason}", file=sys.stderr)
                return 1
            profiles: list[TableProfile] = []
            if args.profile:
                from services.lenses.profiler import profile_connection

                try:
                    profiles = profile_connection(connector, args.connection, tables)
                except Exception as exc:  # noqa: BLE001 — a failed sample must not eat the schema
                    first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                    print(
                        f"warning: --profile did not complete ({first}) — printing schema only",
                        file=sys.stderr,
                    )
            if args.check_joins:
                return _check_joins(connector, Path(args.dir))
            if args.as_json:
                print(json.dumps(schema_json(snapshot, profiles, tables), indent=2))
            else:
                print(serialize_schema(snapshot, profiles, tables))
            return 0

    url, headers = _client(args)
    params = {"connection": args.connection}
    if args.tables:
        params["tables"] = args.tables
    r = httpx.get(f"{url}/mgmt/semantic/introspect", headers=headers, params=params, timeout=120)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = r.text[:200]
        if r.status_code == 400:  # unknown to the server AND undeclared in the file
            detail = _undeclared_connection(args)
        print(f"error: {detail}", file=sys.stderr)
        return 1
    payload = r.json()
    if args.as_json:
        structured = payload.get("json")
        if structured is None:
            print(
                f"error: {url} does not serve --json for introspect (it predates the "
                "structured listing) — drop --json, or upgrade the server",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(structured, indent=2))
        return 0
    text = payload["text"]
    if not text.strip():
        print(f"error: connection '{args.connection}' listed no tables", file=sys.stderr)
        return 1
    print(text)
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "introspect",
        help="schema + profile facts for a connection, agent-legible — author "
        "semantic/ files from it with your own agent (see `dst sql` for actual rows)",
    )
    p.add_argument("--connection", required=True)
    p.add_argument("--tables", help="comma-separated table subset")
    p.add_argument(
        "--check-joins",
        action="store_true",
        help="measure each declared join's real cardinality against the warehouse and "
        "report declarations the data contradicts. The compiler trusts `relationship` "
        "to decide whether a join is safe to emit, so a wrong one silently multiplies "
        "every aggregate; exits non-zero when a declaration claims safety the data denies",
    )
    p.add_argument(
        "--profile",
        action="store_true",
        help="sample the warehouse now for enum values, null rates and ranges — "
        "row-capped read-only reads, one pass per table in scope (narrow it with "
        "--tables). Without it the listing is schema only and says so. Applies to "
        "the dst.yaml path — the server always answers with what it stored",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the listing as a parseable object instead of prose",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose dst.yaml declares the connection (no apply needed)",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_introspect)
