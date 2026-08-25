"""Generic OIDC dashboard auth — the bring-your-own-IdP path.

Verifies an OIDC ID/access token against the issuer's JWKS (issuer + audience +
signature), maps the user to a dst org and groups, and resolves an identity the
same way Clerk does — so `policy.authorize` and everything downstream is unchanged.
This is what lets a self-hoster point dst at Keycloak / Authentik / Zitadel /
Okta / Entra / Google without a hosted vendor in the path.

Sits BESIDE Clerk in the resolver fall-through (services/auth/deps.py), enabled by
`DST_OIDC_ISSUER`. Discovery (`/.well-known/openid-configuration`) finds the
`jwks_uri` because IdPs disagree on its path; `DST_OIDC_JWKS_URL` overrides it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import text

from services.config import settings
from services.db.session import admin_engine


@dataclass
class OidcIdentity:
    org_id: uuid.UUID
    user: str
    is_admin: bool
    groups: list[str] = field(default_factory=list)


def is_configured() -> bool:
    return bool(settings.oidc_issuer)


@lru_cache(maxsize=1)
def _jwks_url() -> str | None:
    """The JWKS URL: explicit override, else discovered from the issuer.

    Cached — discovery is one network round trip per process. A discovery failure
    returns None (token verification then fails closed) rather than raising at import,
    so a misconfigured issuer degrades to "OIDC login doesn't work", not "server won't
    boot"."""
    if settings.oidc_jwks_url:
        return settings.oidc_jwks_url
    issuer = settings.oidc_issuer
    if not issuer:
        return None
    try:
        resp = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=5.0)
        resp.raise_for_status()
        jwks_uri = resp.json().get("jwks_uri")
        return str(jwks_uri) if jwks_uri else None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient | None:
    url = _jwks_url()
    return PyJWKClient(url) if url else None


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify signature, issuer, and audience. Any failure ⇒ None (fail closed).

    Audience is enforced, never skipped: an omitted `aud` check against a
    multi-tenant IdP is the documented "a token minted for any other app validates
    here" hole. If `oidc_audience` is unset we refuse rather than verify without it."""
    if not settings.oidc_issuer or not settings.oidc_audience:
        return None
    client = _jwks_client()
    if client is None:
        return None
    try:
        signing_key = client.get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "ES256"],
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
        )
        return claims
    except Exception:
        return None


def org_for_oidc(ref: str, name: str) -> uuid.UUID:
    """Idempotent org provisioning keyed by a stable issuer ref — mirrors org_for_clerk."""
    with admin_engine.begin() as conn:
        row = conn.execute(text("SELECT id FROM org WHERE oidc_ref = :r"), {"r": ref}).first()
        if row is not None:
            return uuid.UUID(str(row[0]))
        new_id = conn.execute(
            text("INSERT INTO org (name, oidc_ref) VALUES (:n, :r) RETURNING id"),
            {"n": name, "r": ref},
        ).scalar_one()
        return uuid.UUID(str(new_id))


def _groups(claims: dict[str, Any]) -> list[str]:
    raw = claims.get(settings.oidc_groups_claim)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(g) for g in raw]
    return []


def resolve_identity(token: str) -> OidcIdentity | None:
    """Verify an OIDC token and resolve a dst identity, provisioning the org.

    All users of one issuer share one org (the self-host single-company case); the org
    is keyed by the issuer so it is stable across restarts. Admin is granted only to
    members of `DST_OIDC_ADMIN_GROUP` — unset means nobody is admin via OIDC, which
    is the safe default: the operator names the privileged IdP group deliberately."""
    claims = verify_token(token)
    if claims is None:
        return None
    ref = f"oidc:{settings.oidc_issuer}"
    org_id = org_for_oidc(ref, settings.oidc_org)
    user = str(
        claims.get("email") or claims.get("preferred_username") or claims.get("sub") or "user"
    )
    groups = _groups(claims)
    is_admin = settings.oidc_admin_group is not None and settings.oidc_admin_group in groups
    return OidcIdentity(org_id=org_id, user=user, is_admin=is_admin, groups=groups)
