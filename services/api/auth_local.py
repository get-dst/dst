"""Local dashboard login (self-host, no Clerk): /auth/* + admin /mgmt/users.

Login returns the `dstsess_` token in the body AND sets it as an httpOnly cookie,
so the same-origin SPA can rely on the cookie while API clients use the bearer
header (the header always wins when both are present — see auth/deps.py).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth import local
from services.auth.deps import SESSION_COOKIE, get_app_session
from services.auth.tokens import SESSION_PREFIX
from services.config import settings
from services.governance import ratelimit

router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/mgmt/users", tags=["users"])

# Online password guessing against a known email has no lockout otherwise (each
# valid-email attempt also costs one 32MiB scrypt server-side). Keyed by email so a
# spray across many accounts isn't throttled as one — it's the per-account guess we cap.
_LOGIN_RPM = 10


class LoginBody(BaseModel):
    email: str
    password: str


def _session_token(authorization: str | None, cookie: str | None) -> str | None:
    """The active `dstsess_` token: bearer header first, then the cookie."""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization.removeprefix("Bearer ").strip()
        return raw if raw.startswith(SESSION_PREFIX) else None
    return cookie if cookie and cookie.startswith(SESSION_PREFIX) else None


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, str | None]:
    """Email + password → a fresh `dstsess_` session token, in the body and as an
    httpOnly cookie — the same-origin SPA rides the cookie, API clients the bearer
    header. 401 on bad credentials."""
    key = f"login:{body.email.strip().lower()}"
    if not ratelimit.check(key, _LOGIN_RPM):
        raise HTTPException(
            status_code=429,
            detail="too many login attempts — wait a moment",
            headers={"Retry-After": str(ratelimit.retry_after(key))},
        )
    raw = local.login(body.email, body.password)
    if raw is None:
        raise HTTPException(status_code=401, detail="invalid email or password")
    ident = local.resolve(raw)
    assert ident is not None  # freshly minted
    expires = local.session_expiry(raw)
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(local.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        # Behind a TLS-terminating proxy that dst isn't told to trust (Cloud Run, etc.)
        # request.url.scheme reads 'http', which would drop Secure on a live HTTPS site.
        # In production the cookie is always Secure regardless of the observed scheme.
        secure=request.url.scheme == "https" or settings.environment == "production",
    )
    return {
        "token": raw,
        "expires_at": expires.isoformat() if expires else None,
        "email": ident.user,
        "role": "admin" if ident.is_admin else "member",
        "org_id": str(ident.org_id),
    }


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    authorization: str | None = Header(default=None),
    dst_session: str | None = Cookie(default=None),
) -> None:
    """Revoke the active session (header or cookie) and clear the cookie.
    Idempotent — signed out is still a 204."""
    raw = _session_token(authorization, dst_session)
    if raw:
        local.logout(raw)
    response.delete_cookie(SESSION_COOKIE)


@router.get("/me")
def me(
    authorization: str | None = Header(default=None),
    dst_session: str | None = Cookie(default=None),
) -> dict[str, str]:
    """Who the active session belongs to — email, role, org. 401 when not signed in."""
    raw = _session_token(authorization, dst_session)
    ident = local.resolve(raw) if raw else None
    if ident is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return {
        "email": ident.user,
        "role": "admin" if ident.is_admin else "member",
        "org_id": str(ident.org_id),
    }


class UserBody(BaseModel):
    email: str
    password: str
    role: Literal["admin", "member"] = "member"


@users_router.post("", status_code=201)
def create_user(body: UserBody, session: Session = Depends(get_app_session)) -> dict[str, str]:
    """Create a local dashboard user (admin or member). 409 when the email is taken."""
    if local.user_id_by_email(session, body.email) is not None:
        raise HTTPException(status_code=409, detail=f"user '{body.email}' already exists")
    uid = local.create_user(session, body.email, body.password, body.role)
    return {"id": str(uid), "email": body.email, "role": body.role}


@users_router.get("")
def list_users(session: Session = Depends(get_app_session)) -> list[dict[str, str]]:
    """Every local dashboard user — id, email, role."""
    rows = session.execute(text("SELECT id, email, role FROM local_user ORDER BY email")).all()
    return [{"id": str(r[0]), "email": r[1], "role": r[2]} for r in rows]
