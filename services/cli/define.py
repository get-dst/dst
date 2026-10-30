"""The `dst define` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from services.cli._common import (
    _client,
    _detail,
)


def _define(args: argparse.Namespace) -> int:
    """Print what a governed term MEANS — the read door as a one-liner.

    The sibling of `dst query`: query answers questions ABOUT DATA (governed
    SQL, a warehouse execution); define returns the approved definition verbatim —
    no generation, no warehouse, nothing billed. Deliberately an index lookup, not
    a search: top-k retrieval over the same governed vocabulary returns near
    neighbours and can miss the exact term that was asked for, which is the one
    thing this verb must never do. Exit 1 when nothing is governed, so agents
    branch on the code instead of parsing prose."""
    import httpx

    url, headers = _client(args, caller_key_ok=True)
    r = httpx.get(
        f"{url}/v1/definitions", headers=headers, params={"q": args.term}, timeout=args.timeout
    )
    if r.status_code >= 400:
        print(f"error: {_detail(r)}", file=sys.stderr)
        return 1
    payload = r.json()
    hits = payload.get("definitions") or []
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if hits else 1
    if not hits:
        # Mirrors the MCP tool's empty-case note: an ungoverned term is a fact to
        # report, never a meaning to invent.
        print(f"no governed definition mentions '{args.term}'", file=sys.stderr)
        return 1
    for i, d in enumerate(hits):
        if i:
            print()
        badge = "" if d.get("status") == "active" else f" [{d.get('status')}]"
        print(f"{d['term']}{badge}  (lens: {d['lens']})")
        for line in (d.get("body") or "").strip().splitlines():
            print(f"  {line}")
        if d.get("possible_mappings"):
            print(f"  possible mappings: {', '.join(d['possible_mappings'])}")
        if d.get("cites"):
            print(f"  cites: {', '.join(d['cites'])}")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("define", help="print a governed term's meaning — no SQL, no warehouse")
    p.add_argument("term")
    p.add_argument("--json", action="store_true", help="full DefinitionLookup JSON")
    p.add_argument("--timeout", type=int, default=30)
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
    p.add_argument(
        "--key",
        help="look up AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env); you see terms only from lenses your key may use",
    )
    p.set_defaults(fn=_define)
