"""Local dashboard auth (self-host, no Clerk): email+password -> `dstsess_` session.

Passwords use scrypt (hashlib, per-user salt); sessions are opaque tokens stored
by hash in `local_session` with a fixed expiry (no sliding renewal — re-login
after SESSION_TTL). Like `api_key`, session lookup happens before org context
exists, so it goes through the admin engine; `local_user` rows are org-scoped
(RLS) and written through a tenant session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth.tokens import hash_token, new_session_token
from services.db.session import admin_engine

SESSION_TTL = timedelta(days=30)

# scrypt parameters (OWASP-recommended interactive-login cost). OpenSSL caps
# scrypt memory at 32MiB by default and 128*N*r is exactly that, so raise maxmem.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 2**26


@dataclass
class LocalIdentity:
    """Mirrors ClerkIdentity so deps.py treats both dashboards the same."""

    org_id: uuid.UUID
    user: str  # email
    is_admin: bool
    groups: list[str] = field(default_factory=list)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=64,
    )
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=64,
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def create_user(session: Session, email: str, password: str, role: str = "member") -> uuid.UUID:
    """Create a local user in the current org (RLS session). Caller checks duplicates."""
    uid = session.execute(
        text(
            "INSERT INTO local_user (org_id, email, password_hash, role) VALUES "
            "(NULLIF(current_setting('app.current_org', true), '')::uuid, :e, :h, :r) "
            "RETURNING id"
        ),
        {"e": email, "h": hash_password(password), "r": role},
    ).scalar_one()
    return uuid.UUID(str(uid))


def user_id_by_email(session: Session, email: str) -> uuid.UUID | None:
    row = session.execute(text("SELECT id FROM local_user WHERE email = :e"), {"e": email}).first()
    return uuid.UUID(str(row[0])) if row else None


def login(email: str, password: str) -> str | None:
    """Verify credentials and mint a `dstsess_` session token (shown once, stored hashed).

    Pre-org lookup (there is no org context yet): email is unique per org, not
    globally, so candidates across orgs are checked against the password — in the
    single-org OSS install there is exactly one.

    Per-org queries, not one global scan: `local_user` is FORCE-RLS and the
    managed-Postgres admin role has no BYPASSRLS, so an unscoped SELECT sees
    nothing. `org` carries no RLS — walk it and read each org under its GUC.
    """
    with admin_engine.begin() as conn:
        org_ids = conn.execute(text("SELECT id FROM org ORDER BY created_at")).scalars().all()
        for oid in org_ids:
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(oid)})
            rows = conn.execute(
                text(
                    "SELECT id, org_id, password_hash FROM local_user WHERE email = :e "
                    "ORDER BY created_at"
                ),
                {"e": email},
            ).all()
            for row in rows:
                if verify_password(password, row[2]):
                    raw = new_session_token()
                    conn.execute(
                        text(
                            "INSERT INTO local_session (org_id, user_id, token_hash, expires_at) "
                            "VALUES (:o, :u, :h, now() + :ttl)"
                        ),
                        {"o": row[1], "u": row[0], "h": hash_token(raw), "ttl": SESSION_TTL},
                    )
                    return raw
    return None


def resolve(raw: str) -> LocalIdentity | None:
    """Session token -> identity (None if unknown, expired, or revoked).

    `local_session` (no RLS, by design: looked up by hash before org context
    exists) resolves the org first; `local_user` is FORCE-RLS, so it is read
    under that org's GUC — a join would return nothing on a managed-Postgres
    admin role with no BYPASSRLS.
    """
    with admin_engine.begin() as conn:
        sess = conn.execute(
            text(
                "SELECT org_id, user_id FROM local_session "
                "WHERE token_hash = :h AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"h": hash_token(raw)},
        ).first()
        if sess is None:
            return None
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(sess[0])})
        row = conn.execute(
            text("SELECT org_id, email, role FROM local_user WHERE id = :u"), {"u": sess[1]}
        ).first()
    if row is None:
        return None
    role = str(row[2])
    return LocalIdentity(
        org_id=uuid.UUID(str(row[0])),
        user=str(row[1]),
        is_admin=role == "admin",
        groups=[f"local:{role}"],
    )


def logout(raw: str) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE local_session SET revoked_at = now() "
                "WHERE token_hash = :h AND revoked_at IS NULL"
            ),
            {"h": hash_token(raw)},
        )


def session_expiry(raw: str) -> datetime | None:
    """The active session's expiry (for the login response / cookie max-age)."""
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT expires_at FROM local_session WHERE token_hash = :h"),
            {"h": hash_token(raw)},
        ).first()
    return row[0] if row else None
