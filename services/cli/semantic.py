"""The `dst semantic` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from services.cli._common import (
    _client,
)


def _semantic(args: argparse.Namespace) -> int:
    """`dst semantic get|rm <kind> <name>` — read back or delete one shared
    asset. `get` prints the stored body as YAML, so confirming a declaration
    landed needs no direct database query; `rm` is the explicit
    deletion act plan's orphan listing points at."""
    import httpx

    url, headers = _client(args)
    if args.action == "get":
        r = httpx.get(f"{url}/mgmt/semantic/{args.kind}/{args.name}", headers=headers, timeout=60)
        if r.status_code == 404:
            print(f"no {args.kind} '{args.name}'", file=sys.stderr)
            return 1
        r.raise_for_status()
        body = r.json().get("body") or {}
        if getattr(args, "as_json", False):
            print(json.dumps(body, indent=2, ensure_ascii=False))
        else:
            import yaml

            print(yaml.safe_dump(body, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    r = httpx.delete(f"{url}/mgmt/semantic/{args.kind}/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no {args.kind} '{args.name}'", file=sys.stderr)
        return 1
    if r.status_code == 409:  # a published lens still selects it — server names them
        print(f"error: {r.json()['detail']}", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(f"removed {args.kind} '{args.name}'")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "semantic",
        help="manage shared semantic assets on the server: `get` reads back a "
        "published asset (YAML), `rm` deletes one",
    )
    p.add_argument("action", choices=["get", "rm"])
    p.add_argument("kind", choices=["entity", "definition", "relationship", "blueprint"])
    p.add_argument("name")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="the stored body as JSON")
    p.set_defaults(fn=_semantic)
