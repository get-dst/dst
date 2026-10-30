"""Secrets and credentials: `dst secret`, `dst rotate-key`, `dst keys`, `dst revoke-key`,
`dst revoke-token`."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from services.cli._common import (
    _adopt_project_env,
    _client,
    _project_org,
)


def _secret(args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def _rotate_key(args: argparse.Namespace) -> int:
    """`dst rotate-key` — re-encrypt every stored secret under the primary key.

    Step two of a three-step rotation, and the step that was missing entirely:

        DST_SECRET_KEY=<new>,<old>   deploy — everything still decrypts
        dst rotate-key               this verb
        DST_SECRET_KEY=<new>         drop the old key

    Refuses to run with a single key configured. Not pedantry: with only <new>
    set, nothing encrypted under <old> can be read, so the "rotation" would report
    success having silently skipped every row it could not decrypt — the exact
    shape of Metabase's rotation bug, where a partial re-encrypt looked clean.
    """
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.db.session import admin_engine
    from services.security import crypto
    from services.security.sentinel import rotate_sentinel

    crypto.reset_cache()
    if not crypto.is_configured():
        print("error: DST_SECRET_KEY is not set", file=sys.stderr)
        return 1
    if crypto.key_count() < 2 and not args.force:
        print(
            "error: only one key in DST_SECRET_KEY. Rotation needs both:\n"
            "  DST_SECRET_KEY=<new>,<old>\n"
            "Rotating with one key would skip every row it cannot decrypt and still "
            "report success. Pass --force only to re-encrypt under the same key.",
            file=sys.stderr,
        )
        return 1

    rotated = failed = 0
    with admin_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, secret_encrypted FROM connection WHERE secret_encrypted IS NOT NULL")
        ).all()
        for row_id, blob in rows:
            try:
                conn.execute(
                    text("UPDATE connection SET secret_encrypted = :v WHERE id = :i"),
                    {"v": crypto.rotate(str(blob)), "i": row_id},
                )
                rotated += 1
            except crypto.CryptoNotConfigured:
                # Named, never swallowed: a row we cannot decrypt is a row whose
                # key is not in the list, and the operator has to know before they
                # drop the old key.
                print(f"  connection {row_id}: NOT decryptable with any configured key")
                failed += 1
    rotate_sentinel()

    print(f"re-encrypted {rotated} stored secret(s) under the primary key")
    if failed:
        print(
            f"{failed} row(s) could not be decrypted — do NOT drop the old key yet",
            file=sys.stderr,
        )
        return 1
    print("safe to drop the old key from DST_SECRET_KEY")
    return 0


def _keys(args: argparse.Namespace) -> int:
    import httpx

    url, headers = _client(args)
    if args.action == "create":
        if not args.caller:
            print("error: --caller is required for create", file=sys.stderr)
            return 1
        groups = list(args.group or [])
        r = httpx.post(
            f"{url}/mgmt/callers",
            headers=headers,
            json={"name": args.caller, "groups": groups},
            timeout=60,
        )
        if r.status_code not in (201, 409):
            r.raise_for_status()
        if r.status_code == 409 and args.group is not None:
            # An existing caller keeps its groups unless --group was passed.
            httpx.put(
                f"{url}/mgmt/callers/{args.caller}/groups",
                headers=headers,
                json={"groups": groups},
                timeout=60,
            ).raise_for_status()
        if args.driver:
            httpx.put(
                f"{url}/mgmt/callers/{args.caller}/driver",
                headers=headers,
                json={"driver": True},
                timeout=60,
            ).raise_for_status()
        r = httpx.post(f"{url}/mgmt/callers/{args.caller}/keys", headers=headers, timeout=60)
        key = r.raise_for_status().json()
        print(
            f"caller: {args.caller}"
            + (f" (groups: {', '.join(groups)})" if groups else "")
            + (" · driver" if args.driver else "")
        )
        print(f"key (store it now — shown once): {key['key']}")
        return 0
    r = httpx.get(f"{url}/mgmt/callers", headers=headers, timeout=60)
    for c in r.raise_for_status().json():
        print(
            c.get("name"),
            "·",
            ", ".join(c.get("groups") or []) or "no groups",
            *(["· driver"] if c.get("driver") else []),
        )
    return 0


def _revoke_key(args: argparse.Namespace) -> int:
    """`dst revoke-key --caller NAME` — revoke that caller's active keys, in
    ONE org.

    It used to run `UPDATE api_key … WHERE caller.name = :n` on the admin engine
    with no org predicate, so revoking `alex` from the `demoland` project also
    revoked `alex` in `me`: `revoked 2 active key(s)`, measured. Every other
    caller verb goes through /mgmt/callers, which is org-scoped by the admin
    token; this one bypassed the server and wrote raw SQL, so it inherited no
    scope at all. Revocation is an access-control operation — reaching into a
    tenant the operator never named is the one thing it must not do.

    Scoped by ``_project_org``, and the org is NAMED in the output so the
    operator can see what they just did. Unscopable is REFUSED, never widened:
    the read-only sweep `dst test` can afford to say "every org" out loud
    and carry on, and this cannot."""
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.db.session import admin_engine

    scoped = _project_org(args)
    if scoped is None:
        print(
            "error: no org to revoke in — pass --org NAME, or run from a project whose "
            ".env carries DST_ADMIN_TOKEN (`dst bootstrap` writes it). Revoking "
            "across every org in the database is not a default",
            file=sys.stderr,
        )
        return 1
    org_id, org_name = scoped
    with admin_engine.begin() as c:
        # caller is FORCE-RLS: without the org GUC a managed-Postgres admin role
        # (no BYPASSRLS) matches zero callers and the revoke silently no-ops.
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
        n = c.execute(
            text(
                "UPDATE api_key SET revoked_at = now() FROM caller "
                "WHERE api_key.caller_id = caller.id AND caller.name = :n "
                "AND api_key.org_id = :o AND api_key.revoked_at IS NULL"
            ),
            {"n": args.caller, "o": org_id},
        ).rowcount
    print(f"revoked {n} active key(s) for caller '{args.caller}' in org '{org_name}'")
    return 0 if n else 1


def _revoke_token(args: argparse.Namespace) -> int:
    """`dst revoke-token <raw>` — kill one credential, given the credential itself.

    For the case a `dstadm_` token reaches somewhere it should not — a tracked
    file, a log, a chat message. `revoke-key` cannot help: it takes a caller name
    and only touches `api_key`, and admin tokens have no caller.
    The documented remedy was "revoke by hash" — i.e. a hand-written UPDATE against
    the admin engine, at the exact moment an operator is rattled and least likely to
    get a WHERE clause right.

    Takes the RAW token because that is what a leak hands you: the string in the
    file, the CI log, the screenshot. Hashing it here means the caller never has to
    know how we store credentials, and never has to match one by substring.

    Deliberately org-agnostic, unlike `revoke-key`. That verb refuses to act without
    a named org because a caller NAME is ambiguous across tenants and over-revoking
    is its failure mode. A token hash is unique by construction (`UNIQUE` on both
    columns), so there is exactly one row it can touch in exactly one org — naming
    the org would add a way to fail, not a guard. The org is printed so the operator
    sees which tenant they just touched.
    """
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.auth.tokens import hash_token
    from services.db.session import admin_engine

    token_hash = hash_token(args.token.strip())
    # Both stores: the operator should not have to know which one a prefix implies,
    # and `dsto_` OAuth tokens live in api_key beside `dst_` caller keys.
    targets = (("admin_token", "token_hash"), ("api_key", "key_hash"))
    with admin_engine.begin() as c:
        for table, column in targets:
            row = c.execute(
                text(
                    f"UPDATE {table} SET revoked_at = now() "  # noqa: S608 — literals, not input
                    f"WHERE {column} = :h AND revoked_at IS NULL RETURNING org_id"
                ),
                {"h": token_hash},
            ).first()
            if row is not None:
                org = c.execute(text("SELECT name FROM org WHERE id = :o"), {"o": row[0]}).scalar()
                print(f"revoked 1 {table} in org '{org}'")
                return 0
        # "Already dead" and "never existed here" mean opposite things to an operator
        # racing to contain a leak — one says stop, the other says you are looking in
        # the wrong database. Never collapse them into a single success.
        for table, column in targets:
            if c.execute(
                text(f"SELECT 1 FROM {table} WHERE {column} = :h"),  # noqa: S608 — literals
                {"h": token_hash},
            ).first():
                print(f"already revoked ({table})")
                return 0
    print(
        "no such credential in this database — check DATABASE_URL points at the "
        "deployment this token belongs to",
        file=sys.stderr,
    )
    return 1


def register_secret(sub: Any) -> None:
    sub.add_parser("secret", help="generate a DST_SECRET_KEY (Fernet)").set_defaults(fn=_secret)


def register_rotate_key(sub: Any) -> None:
    p = sub.add_parser("rotate-key", help="re-encrypt stored secrets under a new DST_SECRET_KEY")
    p.add_argument(
        "--force",
        action="store_true",
        help="allow rotation with a single configured key (re-encrypt in place)",
    )
    p.add_argument("--dir", default=".", help="project root whose .env supplies the keys")
    p.set_defaults(fn=_rotate_key)


def register_keys(sub: Any) -> None:
    p = sub.add_parser("keys", help="create a caller + key, or list callers")
    p.add_argument("action", choices=["create", "list"])
    p.add_argument("--caller", help="caller name (required for create)")
    p.add_argument(
        "--group",
        action="append",
        help="a group the caller belongs to (repeatable; replaces an existing "
        "caller's groups) — what a lens's access.allow and a hide entry's "
        "except_groups read",
    )
    p.add_argument(
        "--driver",
        action="store_true",
        help="mark the caller a driver: an agent that writes its own Cube queries and "
        "SQL, so dst calls no model for its requests to lenses served through Cube",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_keys)


def register_revoke_key(sub: Any) -> None:
    p = sub.add_parser("revoke-key", help="revoke a caller's active API keys, in ONE org")
    p.add_argument("--caller", required=True)
    p.add_argument(
        "--org",
        help="the org to revoke in (default: the org this project's DST_ADMIN_TOKEN "
        "belongs to). Caller names are not unique across orgs — this command never "
        "revokes in an org you did not name",
    )
    p.add_argument("--dir", default=".", help="project root whose .env supplies the token")
    p.set_defaults(fn=_revoke_key)


def register_revoke_token(sub: Any) -> None:
    p = sub.add_parser(
        "revoke-token",
        help="revoke ONE credential by its raw value (dstadm_/dst_/dsto_) — for leaks",
    )
    p.add_argument("token", help="the leaked credential itself, as it appears in the file/log")
    p.add_argument("--dir", default=".", help="project root whose .env supplies DATABASE_URL")
    p.set_defaults(fn=_revoke_token)
