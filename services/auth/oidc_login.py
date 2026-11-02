"""The MCP consent step's sign-in against the configured OIDC issuer.

The consent page sends the person to the IdP (authorization code + PKCE, a
nonce, a signed pending-request cookie), the IdP sends them back to
``/oauth/callback`` with a code, and this module turns that code into a dst
caller: the ID token is verified (``services.auth.oidc.verify_id_token``:
signature over JWKS, issuer, audience, expiry, nonce), the caller for its
subject is created or reused, the caller's groups are refreshed through the
org's declared mapping, and ``services.api.oauth`` mints the client's auth
code from there as it does for a pasted key.

Everything here fails closed: no discovery document, no sign-in; no cookie,
no state match, no verified token, no caller. The IdP's codes and tokens are
never logged. Deprovisioning is ``sync_idp``: the callers whose subject the
IdP's user listing no longer carries lose every key.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth import oidc
from services.auth.oauth import _signing_secret, pkce_s256_challenge
from services.config import settings
from services.db.session import org_session
from services.governance import credentials
from services.project import caller_groups

log = logging.getLogger("dst.oidc")

# The pending OAuth request rides a signed, httpOnly cookie across the round
# trip to the IdP: the client's parameters, our PKCE verifier toward the IdP,
# the nonce, and the state the IdP must echo. Binding it to the browser (rather
# than carrying it in `state`) is what stops a login-CSRF: a callback built from
# someone else's IdP sign-in has no cookie to pair with.
PENDING_COOKIE = "dst_oidc"
PENDING_TTL_SECONDS = 600
_PENDING_TYP = "dst-oidc-pending"
_ALG = "HS256"
CALLBACK_PATH = "/oauth/callback"
# What `services.api.oauth` stamps on the auth code and reads back at the token
# endpoint to pick the shorter lifetime.
IDP_TAG = "oidc"


def configured() -> bool:
    """Issuer, audience and a client id: the sign-in needs all three."""
    return bool(settings.oidc_issuer and settings.oidc_audience and oidc.client_id())


def start(params: dict[str, str], callback_url: str) -> tuple[str, str] | None:
    """The IdP authorization URL to send the browser to and the pending cookie
    to set with it, or None when the issuer cannot be reached (no discovery
    document, or one without an authorization endpoint)."""
    if not configured():
        return None
    endpoint = oidc.discovery().get("authorization_endpoint")
    if not endpoint:
        return None
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(24)
    now = int(time.time())
    pending = jwt.encode(
        {
            "typ": _PENDING_TYP,
            "params": params,
            "v": verifier,
            "nonce": nonce,
            "state": state,
            "iat": now,
            "exp": now + PENDING_TTL_SECONDS,
        },
        _signing_secret(),
        algorithm=_ALG,
    )
    query = {
        "response_type": "code",
        "client_id": oidc.client_id() or "",
        "redirect_uri": callback_url,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_s256_challenge(verifier),
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in str(endpoint) else "?"
    return f"{endpoint}{sep}{urlencode(query)}", pending


def verify_pending(cookie: str | None, state: str) -> dict[str, Any] | None:
    """The pending request the cookie carries, when its signature holds, it has
    not expired, and the state the IdP echoed is the one it was started with."""
    if not cookie or not state:
        return None
    try:
        claims: dict[str, Any] = jwt.decode(cookie, _signing_secret(), algorithms=[_ALG])
    except Exception:
        return None
    if claims.get("typ") != _PENDING_TYP or not isinstance(claims.get("params"), dict):
        return None
    if not secrets.compare_digest(str(claims.get("state") or ""), state):
        return None
    return claims


def exchange(code: str, verifier: str, callback_url: str) -> str | None:
    """Redeem the IdP's code for an ID token at its token endpoint. The token
    is returned unverified: ``oidc.verify_id_token`` is the next step, always."""
    endpoint = oidc.discovery().get("token_endpoint")
    if not endpoint:
        return None
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "client_id": oidc.client_id() or "",
        "code_verifier": verifier,
    }
    if settings.oidc_client_secret:
        form["client_secret"] = settings.oidc_client_secret
    try:
        resp = httpx.post(str(endpoint), data=form, timeout=10.0)
        if resp.status_code != 200:
            # Status only: the body may echo the code or carry the IdP's own detail.
            log.warning("oidc: token endpoint answered %s", resp.status_code)
            return None
        id_token = resp.json().get("id_token")
    except Exception:
        log.warning("oidc: token endpoint unreachable or unparseable")
        return None
    return str(id_token) if id_token else None


def complete(
    code: str, pending: dict[str, Any], callback_url: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Code -> ID token -> verified claims -> (org_id, caller_id); None at the
    first step that fails."""
    id_token = exchange(code, str(pending["v"]), callback_url)
    if not id_token:
        return None
    claims = oidc.verify_id_token(id_token, str(pending["nonce"]))
    if claims is None:
        return None
    return resolve_caller(claims)


def resolve_caller(claims: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(org_id, caller_id) for a verified ID token's subject, creating the caller
    on first sign-in and refreshing its groups on every one.

    Reuse is by subject, not name: an email change at the IdP is the same person.
    A caller an admin created under the same name and never bound to a subject is
    claimed by the first sign-in with that name, since the name came from a
    verified token. A name already bound to another subject is refused: two
    people would otherwise be one caller.

    Groups: every dst group the org's mapping names belongs to the IdP from here
    on (granted when the token carries a mapped IdP group, dropped when it does
    not); a group with no mapping keeps whatever an admin set
    (``caller_groups.refresh_idp_groups``).
    """
    subject = str(claims.get("sub") or "")
    if not subject:
        return None
    org = oidc.org_id()
    name = oidc.user_name(claims)
    asserted = oidc.idp_groups(claims)
    with org_session(org) as session:
        mapping = caller_groups.load_idp_mapping(session)
        found = session.execute(
            text("SELECT id, groups FROM caller WHERE idp_subject = :s"), {"s": subject}
        ).first()
        if found is not None:
            cid, current = uuid.UUID(str(found[0])), list(found[1] or [])
        else:
            named = session.execute(
                text("SELECT id, groups, idp_subject FROM caller WHERE name = :n"), {"n": name}
            ).first()
            if named is not None and named[2] is not None:
                log.warning("oidc: caller name already bound to another subject")
                return None
            if named is not None:
                cid, current = uuid.UUID(str(named[0])), list(named[1] or [])
                session.execute(
                    text("UPDATE caller SET idp_subject = :s WHERE id = :i"),
                    {"s": subject, "i": cid},
                )
            else:
                cid = credentials.create_caller(
                    session,
                    name,
                    "user",
                    caller_groups.map_idp_groups(mapping, asserted),
                    idp_subject=subject,
                )
                session.commit()
                return org, cid
        groups = caller_groups.refresh_idp_groups(mapping, current, asserted)
        session.execute(
            text("UPDATE caller SET groups = :g WHERE id = :i"), {"g": groups, "i": cid}
        )
        session.commit()
        return org, cid


# --- deprovisioning ---------------------------------------------------------------


class UsersUnavailable(Exception):
    """The IdP's user listing could not be read, so nothing may be revoked."""


def _subjects_page(resp: httpx.Response) -> list[str]:
    body = resp.json()
    rows: Any = body
    if isinstance(body, dict):
        # SCIM answers under `Resources`, Entra under `value`; take the one list.
        rows = next((v for v in body.values() if isinstance(v, list)), [])
    if not isinstance(rows, list):
        raise UsersUnavailable("the users endpoint did not answer with a list")
    field = settings.oidc_users_subject_field
    out: list[str] = []
    for r in rows:
        if isinstance(r, dict) and r.get(field) not in (None, ""):
            out.append(str(r[field]))
    return out


def list_idp_subjects() -> set[str]:
    """Every subject the configured users endpoint lists, following `Link:
    rel="next"` pages (Okta). Raises ``UsersUnavailable`` on any failure, and on
    an empty listing: an IdP with nobody in it is a misconfigured URL, and
    revoking everyone on it would be the sync doing the leak's job in reverse."""
    url = settings.oidc_users_url
    if not url:
        raise UsersUnavailable("DST_OIDC_USERS_URL is not set")
    headers = {"Accept": "application/json"}
    if settings.oidc_users_token:
        headers["Authorization"] = f"Bearer {settings.oidc_users_token}"
    subjects: set[str] = set()
    seen: set[str] = set()
    next_url: str | None = url
    while next_url and next_url not in seen:
        seen.add(next_url)
        try:
            resp = httpx.get(next_url, headers=headers, timeout=15.0)
        except Exception as exc:
            raise UsersUnavailable(
                f"the users endpoint is unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise UsersUnavailable(f"the users endpoint answered {resp.status_code}")
        try:
            subjects.update(_subjects_page(resp))
        except UsersUnavailable:
            raise
        except Exception as exc:
            raise UsersUnavailable("the users endpoint did not answer with JSON") from exc
        nxt = resp.links.get("next", {}).get("url")
        next_url = str(nxt) if nxt else None
    if not subjects:
        raise UsersUnavailable("the users endpoint listed nobody — nothing revoked")
    return subjects


def sync_idp(session: Session, *, dry_run: bool = False) -> dict[str, Any]:
    """Revoke every key of each IdP-bound caller in this org whose subject the
    IdP no longer lists. The session is the org's; ``api_key`` carries no RLS,
    so the revoke names the org itself. Raises ``UsersUnavailable`` rather than
    revoking on a listing it could not trust."""
    listed = list_idp_subjects()
    rows = session.execute(
        text("SELECT id, name, idp_subject FROM caller WHERE idp_subject IS NOT NULL")
    ).all()
    gone = [(r[0], str(r[1])) for r in rows if str(r[2]) not in listed]
    keys_revoked = 0
    if gone and not dry_run:
        keys_revoked = int(
            session.execute(
                text(
                    "UPDATE api_key SET revoked_at = now() "
                    "WHERE caller_id = ANY(:ids) AND revoked_at IS NULL "
                    "AND org_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
                ),
                {"ids": [r[0] for r in gone]},
            ).rowcount  # type: ignore[attr-defined]
        )
        session.commit()
    return {
        "listed": len(listed),
        "callers": len(rows),
        "revoked": sorted(n for _, n in gone),
        "keys_revoked": keys_revoked,
        "dry_run": dry_run,
    }
