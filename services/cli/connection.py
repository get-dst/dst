"""The `dst connection` command."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from services.cli._common import (
    _client,
)


def _connection(args: argparse.Namespace) -> int:
    """`dst connection rm <name>` — the CLI face of DELETE /mgmt/connections.
    Same doctrine as `lens rm`/`semantic rm` (file absence never deletes;
    plan flags server-only connections forever otherwise), same guard style:
    dependents shown BEFORE acting, y/N unless --yes. Refuses while lenses
    still read through the connection — re-point or remove them first."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/connections/{args.name}/dependents", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no connection '{args.name}'", file=sys.stderr)
        return 1
    lenses = r.raise_for_status().json().get("lenses", [])
    if lenses:
        names = ", ".join(str(ln.get("name", "?")) for ln in lenses)
        print(
            f"refusing: {len(lenses)} lens(es) still read through '{args.name}': {names} — "
            "re-point or `dst lens rm` them first",
            file=sys.stderr,
        )
        return 1
    print(f"deleting connection '{args.name}' (stored credentials go with it)")
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing without confirmation — pass --yes", file=sys.stderr)
            return 1
        if input("delete? y/N: ").strip().lower() != "y":
            print("aborted — nothing deleted")
            return 1
    r = httpx.delete(f"{url}/mgmt/connections/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no connection '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(
        f"removed connection '{args.name}' (if dst.yaml still declares it, "
        "the next apply re-creates it)"
    )
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "connection",
        help="rm: delete a server-side connection (dependents checked first) — "
        "the CLI parity for what plan flags as server-only",
    )
    p.add_argument("action", choices=["rm"])
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="skip the confirmation (headless)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_connection)
