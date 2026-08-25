"""FastAPI dependencies for auth + tenant-scoped DB sessions.

- `get_admin_org` / `get_app_session`: control plane (admin token).
- `get_caller`: data plane — accepts a caller API key (governed) OR an admin token
  (acts as a superuser caller). Resolves a CallerIdentity for the runtime + policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth import clerk, local, oidc
from services.auth.tokens import (
    ADMIN_PREFIX,
    CALLER_PREFIX,
    OAUTH_PREFIX,
    SESSION_PREFIX,
    hash_token,
)
from services.db.session import admin_engine, org_session
from services.governance.credentials import CallerIdentity, verify_caller_key

SESSION_COOKIE = "dst_session"


def _bearer(authorization: str | None, session_cookie: str | None = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        # Local dashboard sessions also arrive as an httpOnly cookie (same-origin
        # SPA). An Authorization header always wins; the cookie only fills its absence.
        if session_cookie and session_cookie.startswith(SESSION_PREFIX):
            return session_cookie
        raise HTTPException(status_code=401, detail="missing credentials")
    return authorization.removeprefix("Bearer ").strip()


# An agent label is untrusted free text from a header — bound so a hostile client
# cannot bloat a log row, and stripped so whitespace-only reads as absent.
_AGENT_MAX = 128


def _clean_agent(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    return raw.strip()[:_AGENT_MAX]


def resolve_admin_token(raw: str) -> tuple[uuid.UUID, str | None] | None:
    """Admin token -> (org_id, label). The label is attribution, never authz."""
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT org_id, label FROM admin_token WHERE token_hash = :h AND revoked_at IS NULL"
            ),
            {"h": hash_token(raw)},
        ).first()
    return (uuid.UUID(str(row[0])), row[1]) if row else None


def resolve_admin_org(raw: str) -> uuid.UUID | None:
    resolved = resolve_admin_token(raw)
    return resolved[0] if resolved else None


@dataclass
class AdminIdentity:
    """Control-plane identity: the org plus WHO is acting, for audit trails.

    The actor string follows the trust-tier convention — the prefix is the
    load-bearing part, because trust levels derive from it:
      `human:<email>`  a dashboard session (local or Clerk): a verified person
      `token:<label>`  a raw admin token: full admin power, but not provably a
                       person, so it never claims the `human:` prefix
    """

    org_id: uuid.UUID
    actor: str


def get_admin_identity(
    authorization: str | None = Header(default=None),
    dst_session: str | None = Cookie(default=None),
) -> AdminIdentity:
    raw = _bearer(authorization, dst_session)
    if raw.startswith(ADMIN_PREFIX):
        resolved = resolve_admin_token(raw)
        if resolved is None:
            raise HTTPException(status_code=401, detail="invalid admin token")
        org_id, label = resolved
        return AdminIdentity(org_id=org_id, actor=f"token:{label}" if label else "token")
    if raw.startswith(SESSION_PREFIX):
        local_ident = local.resolve(raw)
        if local_ident is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        if not local_ident.is_admin:
            raise HTTPException(status_code=403, detail="admin role required to manage this org")
        return AdminIdentity(org_id=local_ident.org_id, actor=f"human:{local_ident.user}")
    ident = clerk.resolve_identity(raw)
    if ident is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not ident.is_admin:
        # Control plane (management API) is admin-only; members use the data plane.
        raise HTTPException(status_code=403, detail="admin role required to manage this org")
    return AdminIdentity(org_id=ident.org_id, actor=f"human:{ident.user}")


def get_admin_org(identity: AdminIdentity = Depends(get_admin_identity)) -> uuid.UUID:
    return identity.org_id


def get_app_session(org_id: uuid.UUID = Depends(get_admin_org)) -> Iterator[Session]:
    """Yield an org-scoped, RLS-enforced session (admin-authenticated control plane)."""
    with org_session(org_id) as session:
        yield session


def get_caller(
    authorization: str | None = Header(default=None),
    dst_session: str | None = Cookie(default=None),
    x_dst_agent: str | None = Header(default=None),
) -> CallerIdentity:
    """Data-plane identity: caller API key (governed) or admin token (superuser).

    `X-dst-Agent` names the acting client (set by the MCP server, or by any
    direct caller that wants to identify itself). It is a label attached to the
    resolved identity — never part of resolving it."""
    raw = _bearer(authorization, dst_session)
    agent = _clean_agent(x_dst_agent)
    if raw.startswith(ADMIN_PREFIX):
        org_id = resolve_admin_org(raw)
        if org_id is None:
            raise HTTPException(status_code=401, detail="invalid admin token")
        return CallerIdentity(org_id=org_id, name="admin", is_admin=True, agent=agent)
    if raw.startswith((CALLER_PREFIX, OAUTH_PREFIX)):
        ident = verify_caller_key(raw)
        if ident is None:
            raise HTTPException(status_code=401, detail="invalid caller key")
        return replace(ident, agent=agent)
    if raw.startswith(SESSION_PREFIX):
        local_ident = local.resolve(raw)
        if local_ident is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return CallerIdentity(
            org_id=local_ident.org_id,
            name=local_ident.user,
            is_admin=local_ident.is_admin,
            groups=local_ident.groups,
            agent=agent,
        )
    clerk_ident = clerk.resolve_identity(raw)
    if clerk_ident is not None:
        return CallerIdentity(
            org_id=clerk_ident.org_id,
            name=clerk_ident.user,
            is_admin=clerk_ident.is_admin,
            groups=clerk_ident.groups,
            agent=agent,
        )
    oidc_ident = oidc.resolve_identity(raw)
    if oidc_ident is not None:
        return CallerIdentity(
            org_id=oidc_ident.org_id,
            name=oidc_ident.user,
            is_admin=oidc_ident.is_admin,
            groups=oidc_ident.groups,
            agent=agent,
        )
    raise HTTPException(status_code=401, detail="invalid credentials")


def resolve_mcp_caller(raw: str, *, resource: str | None = None) -> CallerIdentity | None:
    """Resolve a data-plane caller for the MCP transport check.

    `dstadm_` is handled by the middleware (rejected with remediation), so this covers the
    data-plane credentials: a `dst_`/`dsto_` key (governed), a `dstsess_` local session, or
    a Clerk session JWT. None means "not a valid caller" → the transport returns 401 +
    WWW-Authenticate.

    `resource` is this deployment's MCP URL (RFC 8707 audience). A `dsto_` token that
    recorded a different audience at mint time is refused. Honest scope: because tokens
    are opaque rows in our own store, anything we can look up was by definition issued
    by us — this is not closing a live hole. It matters in the one case that is real,
    two deployments sharing a database, and it makes the conformance claim true.
    """
    if raw.startswith((CALLER_PREFIX, OAUTH_PREFIX)):
        return verify_caller_key(raw, resource=resource)
    if raw.startswith(SESSION_PREFIX):
        local_ident = local.resolve(raw)
        if local_ident is None:
            return None
        return CallerIdentity(
            org_id=local_ident.org_id,
            name=local_ident.user,
            is_admin=local_ident.is_admin,
            groups=local_ident.groups,
        )
    clerk_ident = clerk.resolve_identity(raw)
    if clerk_ident is not None:
        return CallerIdentity(
            org_id=clerk_ident.org_id,
            name=clerk_ident.user,
            is_admin=clerk_ident.is_admin,
            groups=clerk_ident.groups,
        )
    oidc_ident = oidc.resolve_identity(raw)
    if oidc_ident is None:
        return None
    return CallerIdentity(
        org_id=oidc_ident.org_id,
        name=oidc_ident.user,
        is_admin=oidc_ident.is_admin,
        groups=oidc_ident.groups,
    )
