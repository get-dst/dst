"""Generic OIDC dashboard auth — the bring-your-own-IdP path.

Verifies an OIDC ID/access token against the issuer's JWKS (issuer + audience +
signature), maps the user to a dst org and groups, and resolves an identity the
same way Clerk does — so `policy.authorize` and everything downstream is unchanged.
This is what lets a self-hoster point dst at Keycloak / Authentik / Zitadel /
Okta / Entra / Google without a hosted vendor in the path.

Sits BESIDE Clerk in the resolver fall-through (services/auth/deps.py), enabled by
`DST_OIDC_ISSUER`. Discovery (`/.well-known/openid-configuration`) finds the
`jwks_uri` because IdPs disagree on its path; `DST_OIDC_JWKS_URL` overrides it.

The MCP consent step's browser sign-in against the same issuer is
``services.auth.oidc_login``; it verifies ID tokens through ``verify_id_token``
here and maps groups the same way, so one mapping rule covers both doors.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import text

from services.config import settings
from services.db.session import admin_engine, org_session
from services.project import caller_groups


@dataclass
class OidcIdentity:
    org_id: uuid.UUID
    user: str
    is_admin: bool
    groups: list[str] = field(default_factory=list)


def is_configured() -> bool:
    return bool(settings.oidc_issuer)


@lru_cache(maxsize=1)
def discovery() -> dict[str, Any]:
    """The issuer's ``/.well-known/openid-configuration``, empty on any failure.

    Cached — discovery is one network round trip per process. A failure returns
    an empty document (token verification and the sign-in redirect then fail
    closed) rather than raising at import, so a misconfigured issuer degrades to
    "OIDC login doesn't work", not "server won't boot"."""
    issuer = settings.oidc_issuer
    if not issuer:
        return {}
    try:
        resp = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=5.0)
        resp.raise_for_status()
        doc = resp.json()
        return dict(doc) if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _jwks_url() -> str | None:
    """The JWKS URL: explicit override, else discovered from the issuer."""
    if settings.oidc_jwks_url:
        return settings.oidc_jwks_url
    jwks_uri = discovery().get("jwks_uri")
    return str(jwks_uri) if jwks_uri else None


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient | None:
    url = _jwks_url()
    return PyJWKClient(url) if url else None


def reset_cache() -> None:
    """Forget the discovered document and signing keys (tests, issuer changes)."""
    discovery.cache_clear()
    _jwks_client.cache_clear()


def client_id() -> str | None:
    """The client id dst was registered under at the IdP: the explicit setting,
    else the audience (the client id at every common IdP)."""
    return settings.oidc_client_id or settings.oidc_audience


def _decode(token: str, *, require: list[str]) -> dict[str, Any] | None:
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
            options={"require": require},
        )
        return claims
    except Exception:
        return None


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify signature, issuer, and audience. Any failure ⇒ None (fail closed).

    Audience is enforced, never skipped: an omitted `aud` check against a
    multi-tenant IdP is the documented "a token minted for any other app validates
    here" hole. If `oidc_audience` is unset we refuse rather than verify without it."""
    return _decode(token, require=["exp"])


def verify_id_token(token: str, nonce: str) -> dict[str, Any] | None:
    """Verify an ID token from the authorization-code exchange: signature,
    issuer, audience, expiry, a subject, and the nonce the sign-in started with.
    The nonce is what ties the token to this browser's request rather than to a
    token the IdP minted for some other sign-in; compared in constant time."""
    claims = _decode(token, require=["exp", "iat", "sub"])
    if claims is None or not nonce:
        return None
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        return None
    return claims


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


def idp_groups(claims: dict[str, Any]) -> list[str]:
    """The groups claim as the IdP asserted it (`DST_OIDC_GROUPS_CLAIM`)."""
    raw = claims.get(settings.oidc_groups_claim)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(g) for g in raw]
    return []


def user_name(claims: dict[str, Any]) -> str:
    return str(
        claims.get("email") or claims.get("preferred_username") or claims.get("sub") or "user"
    )


def org_id() -> uuid.UUID:
    """The one org every user of the configured issuer lands in."""
    return org_for_oidc(f"oidc:{settings.oidc_issuer}", settings.oidc_org)


def dst_groups(org: uuid.UUID, claims: dict[str, Any]) -> list[str]:
    """The dst groups a verified token grants: its IdP groups passed through the
    org's declared mapping (`groups: {support: {idp: [cs-team]}}` in dst.yaml).
    No mapping, no groups: an IdP group name is never a dst group name by itself."""
    with org_session(org) as session:
        mapping = caller_groups.load_idp_mapping(session)
    return caller_groups.map_idp_groups(mapping, idp_groups(claims))


def resolve_identity(token: str) -> OidcIdentity | None:
    """Verify an OIDC token and resolve a dst identity, provisioning the org.

    All users of one issuer share one org (the self-host single-company case); the org
    is keyed by the issuer so it is stable across restarts. Admin is granted only to
    members of `DST_OIDC_ADMIN_GROUP` — unset means nobody is admin via OIDC, which
    is the safe default: the operator names the privileged IdP group deliberately.
    Groups reach dst only through the org's declared mapping."""
    claims = verify_token(token)
    if claims is None:
        return None
    org = org_id()
    asserted = idp_groups(claims)
    is_admin = settings.oidc_admin_group is not None and settings.oidc_admin_group in asserted
    return OidcIdentity(
        org_id=org, user=user_name(claims), is_admin=is_admin, groups=dst_groups(org, claims)
    )
