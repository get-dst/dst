"""The `dst bootstrap` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def _dotenv_value(envfile: Path, name: str) -> str | None:
    """`name=` from one .env file, shell quotes stripped — the file bootstrap
    itself writes, read back before it is rewritten."""
    if not envfile.exists():
        return None
    for line in envfile.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            val = line.split("=", 1)[1].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            return val or None
    return None


def _bootstrap(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from services.auth import tokens
    from services.auth.deps import resolve_admin_token
    from services.db.session import admin_engine

    rotate = bool(getattr(args, "rotate_admin_token", False))
    reset_password = bool(getattr(args, "reset_password", False))

    # Idempotent in the strict sense: rerunning bootstrap reuses the org (oldest
    # wins if legacy duplicates exist), keeps the admin token already saved in
    # ./.env when it is still live for that org, and leaves an existing user
    # alone. It used to mint a token on every run and overwrite the .env line —
    # the earlier token was lost from the file but never revoked — and to reset
    # an existing user's password without being asked.
    envfile = Path(".env")
    env_token = _dotenv_value(envfile, "DST_ADMIN_TOKEN")
    held = resolve_admin_token(env_token) if env_token else None
    with admin_engine.begin() as c:
        org_id = c.execute(
            text("SELECT id FROM org WHERE name = :n ORDER BY created_at LIMIT 1"),
            {"n": args.org},
        ).scalar()
        if held is not None and held[0] != org_id:
            # The token in .env is this project's identity. Pointing .env at
            # another org on a bare rerun is how a project silently moves —
            # refused, naming both sides, so the switch is a decision.
            held_name = c.execute(
                text("SELECT name FROM org WHERE id = :i"), {"i": held[0]}
            ).scalar()
            print(
                f"error: .env holds the admin token of org '{held_name}', but --org names "
                f"'{args.org}' — not switching this project's identity. Rerun with "
                f"--org {held_name}, or remove DST_ADMIN_TOKEN from .env first to "
                f"bootstrap '{args.org}' here.",
                file=sys.stderr,
            )
            return 1
        org_created = org_id is None
        if org_id is None:
            org_id = c.execute(
                text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": args.org}
            ).scalar_one()
        raw = None if held is not None and not rotate else tokens.new_admin_token()
        if raw is not None:
            c.execute(
                text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, :l)"),
                {"o": org_id, "h": tokens.hash_token(raw), "l": "bootstrap"},
            )
    print(f"org: {args.org} ({org_id}) — " + ("created" if org_created else "reused"))

    if raw is None:
        print("admin token: kept the one in .env (--rotate-admin-token mints a new one)")
    else:
        if held is not None:
            print("admin token: rotated — earlier tokens stay valid until revoked")
        elif env_token:
            print("admin token: the DST_ADMIN_TOKEN in .env is not live here — replaced")
        print(f"admin token (store it now — shown once): {raw}")
        if envfile.exists():
            # The project's gitignored secrets file — saving the token here is what
            # lets every later command (and the in-project agent) skip --token.
            kept = [
                line
                for line in envfile.read_text(encoding="utf-8").splitlines()
                if not line.startswith("DST_ADMIN_TOKEN=")
            ]
            kept.append(f"DST_ADMIN_TOKEN={raw}")
            envfile.write_text("\n".join(kept) + "\n", encoding="utf-8")
            print("saved to .env as DST_ADMIN_TOKEN — dst commands here read it automatically")

    if args.email:
        from services.auth import local
        from services.db.session import org_session

        with org_session(org_id) as session:
            uid = local.user_id_by_email(session, args.email)
            if uid is not None and not reset_password:
                print(
                    f"dashboard login: {args.email} already exists — left untouched "
                    "(--reset-password sets a new password and makes it admin)"
                )
                return 0
            password = args.password
            if not password:
                if sys.stdin.isatty():
                    import getpass

                    password = getpass.getpass(f"password for {args.email}: ")
                else:
                    print(
                        "error: --email needs --password (or an interactive terminal)",
                        file=sys.stderr,
                    )
                    return 1
            if uid is None:
                local.create_user(session, args.email, password, role="admin")
            else:
                session.execute(
                    text("UPDATE local_user SET password_hash = :h, role = 'admin' WHERE id = :i"),
                    {"h": local.hash_password(password), "i": uid},
                )
            session.commit()
        print(f"dashboard login: {args.email} (admin)")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "bootstrap",
        help="create (or reuse) an org and its admin token, saved to ./.env; a rerun "
        "keeps the token .env already holds and changes nothing",
    )
    p.add_argument(
        "--url",
        help="ignored — bootstrap talks to DATABASE_URL directly (no server needed)",
    )
    p.add_argument("--org", default="default")
    p.add_argument(
        "--rotate-admin-token",
        action="store_true",
        help="mint a new admin token and replace the one in .env (earlier tokens stay valid)",
    )
    p.add_argument("--email", help="create the first dashboard admin user")
    p.add_argument("--password", help="its password (omit to be prompted)")
    p.add_argument(
        "--reset-password",
        action="store_true",
        help="with --email: set the password of an existing user and make it admin "
        "(without it an existing user is left untouched)",
    )
    p.set_defaults(fn=_bootstrap)
