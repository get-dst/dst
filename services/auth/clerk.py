"""Clerk dashboard auth: verify session JWTs (JWKS) + map a Clerk org -> dst org.

The issuer/JWKS are derived from the publishable key, so only CLERK_PUBLISHABLE_KEY
(and the secret key for the frontend) are needed. A Clerk org (or, if no org is
active, the user) is provisioned to a dst org on first login.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from sqlalchemy import text

from services.config import settings
from services.db.session import admin_engine


@dataclass
class ClerkIdentity:
    org_id: uuid.UUID
    user: str
    is_admin: bool
    groups: list[str] = field(default_factory=list)


def _frontend_host(publishable_key: str) -> str:
    b64 = publishable_key.split("_", 2)[2]
    return base64.b64decode(b64 + "==").decode().rstrip("$")


@lru_cache(maxsize=1)
def issuer() -> str | None:
    if not settings.clerk_publishable_key:
        return None
    return f"https://{_frontend_host(settings.clerk_publishable_key)}"


@lru_cache(maxsize=1)
def _jwks() -> PyJWKClient | None:
    iss = issuer()
    return PyJWKClient(f"{iss}/.well-known/jwks.json") if iss else None


def verify_token(token: str) -> dict[str, Any] | None:
    iss = issuer()
    client = _jwks()
    if not iss or client is None:
        return None
    try:
        signing_key = client.get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=iss,
            options={"verify_aud": False},
        )
        return claims
    except Exception:
        return None


def org_for_clerk(clerk_ref: str, name: str) -> uuid.UUID:
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT id FROM org WHERE clerk_ref = :r"), {"r": clerk_ref}
        ).first()
        if row is not None:
            return uuid.UUID(str(row[0]))
        new_id = conn.execute(
            text("INSERT INTO org (name, clerk_ref) VALUES (:n, :r) RETURNING id"),
            {"n": name, "r": clerk_ref},
        ).scalar_one()
        return uuid.UUID(str(new_id))


def resolve_identity(token: str) -> ClerkIdentity | None:
    """Verify a Clerk JWT and resolve a dst identity, provisioning the org.

    Admin = the Clerk org admin role, or a personal account (sole owner of its tenant).
    Groups carry the Clerk org role (e.g. "org:analyst") so lens allow-lists can grant
    access by role/group rather than naming each member.
    """
    claims = verify_token(token)
    if claims is None:
        return None
    clerk_org = claims.get("org_id")
    ref = str(clerk_org or f"user:{claims.get('sub')}")
    org_name = str(claims.get("org_slug") or claims.get("sub") or "org")
    org_id = org_for_clerk(ref, org_name)
    user = str(claims.get("email") or claims.get("sub") or "clerk-user")

    org_role = claims.get("org_role")  # e.g. "org:admin", "org:member", "org:analyst"
    # Personal account (no org) => sole owner => admin of their own tenant.
    is_admin = (not clerk_org) or (org_role == "org:admin")
    groups = [str(org_role)] if org_role else []
    return ClerkIdentity(org_id=org_id, user=user, is_admin=is_admin, groups=groups)
