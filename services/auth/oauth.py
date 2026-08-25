"""OAuth 2.1 authorization-server facade for the MCP surface.

dst plays the Authorization Server; Clerk is the IdP. The flow is standard
authorization-code + PKCE for a public client:

  register (RFC 7591 DCR) -> authorize -> [Clerk sign-in] -> code -> token (`dsto_`)

This module is the stateless plumbing: PKCE verification, signed one-time auth codes
(no code table — the code is a short-lived HS256 JWT carrying the bound identity +
challenge), and the dynamic-client-registration store. Token minting lives in
``services.governance.credentials.mint_oauth_token`` (reuses the ``api_key`` store so
there is one verification path for both service keys and OAuth tokens). HTTP wiring is
in ``services.api.oauth``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import timedelta
from functools import lru_cache
from typing import Any

import jwt
from sqlalchemy import text

from services.config import settings
from services.db.session import admin_engine

log = logging.getLogger("dst.oauth")

# OAuth access-token lifetime. Long on purpose: revocation is immediate via
# api_key.revoked_at, so a short TTL is not the only safety. Tunable.
OAUTH_TOKEN_TTL = timedelta(days=365)

_CODE_TTL_SECONDS = 120  # authorization code is one-shot and short-lived
_CODE_ALG = "HS256"
_CODE_TYP = "dst-oauth-code"
# Mint-only (never a dispatch key), so client_ids issued under an older prefix
# keep resolving — they are looked up by value, not by prefix.
_CLIENT_PREFIX = "dstc_"


@lru_cache(maxsize=1)
def _dev_secret() -> str:
    log.warning("DST_SECRET_KEY unset; using an ephemeral OAuth code-signing secret")
    return secrets.token_urlsafe(32)


def _signing_secret() -> str:
    # The Fernet key doubles as the HMAC secret for signing auth codes. In real
    # deployments it is set; locally we fall back to an ephemeral per-process secret
    # (pending codes don't survive a restart — fine, they live ~2 min).
    return settings.secret_key or _dev_secret()


# --- PKCE (S256) ----------------------------------------------------------------


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(expected, challenge)


# --- one-time authorization codes (stateless, signed) ---------------------------


def sign_auth_code(
    *,
    caller_id: str,
    org_id: str,
    code_challenge: str,
    redirect_uri: str,
    client_id: str,
    scopes: list[str] | None = None,
    resource: str | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "typ": _CODE_TYP,
        # A unique id per code. Without it "single-use" is unenforceable: two
        # codes minted in the same second for the same client carry identical
        # claims and are indistinguishable in the replay store.
        "jti": secrets.token_urlsafe(16),
        "cid": caller_id,
        "org": org_id,
        "cc": code_challenge,
        "ru": redirect_uri,
        "client": client_id,
        "scope": scopes or [],
        "resource": resource,
        "iat": now,
        "exp": now + _CODE_TTL_SECONDS,
    }
    return jwt.encode(payload, _signing_secret(), algorithm=_CODE_ALG)


def verify_auth_code(code: str) -> dict[str, Any] | None:
    """Signature + type check only. Single-use is `claim_auth_code`, not this."""
    try:
        claims: dict[str, Any] = jwt.decode(code, _signing_secret(), algorithms=[_CODE_ALG])
    except Exception:
        return None
    return claims if claims.get("typ") == _CODE_TYP else None


def claim_auth_code(claims: dict[str, Any]) -> bool:
    """Burn a verified code. True the first time, False on every replay.

    The docstrings called these codes "one-time" three times over and nothing
    enforced it — signature, `typ` and `exp` were the whole check, so a code was
    redeemable repeatedly for its full 120s life. PKCE limits who can exploit that
    (a replayer still needs the verifier), but OAuth 2.1 requires single use, and
    "the other control probably covers it" is how both controls end up missing.

    The INSERT is the lock: `jti` is the primary key, so two concurrent
    redemptions race in Postgres and exactly one wins. Checking-then-inserting
    would leave the window this closes.
    """
    with admin_engine.begin() as conn:
        inserted = conn.execute(
            text(
                "INSERT INTO oauth_code_used (jti, expires_at) "
                "VALUES (:j, to_timestamp(:e)) ON CONFLICT (jti) DO NOTHING"
            ),
            {"j": claims.get("jti"), "e": claims.get("exp", 0)},
        ).rowcount
        if not inserted:
            return False
        # Opportunistic sweep: rows are dead weight once the code they represent
        # would have expired anyway. Cheap on an indexed column, and it keeps the
        # table from growing without bound on a busy instance.
        conn.execute(text("DELETE FROM oauth_code_used WHERE expires_at < now() - interval '1 h'"))
    return True


def revoke_token_from_code(jti: str) -> None:
    """OAuth 2.1: detecting code reuse revokes the token already issued from it.

    A replayed code means the code leaked, which means the token minted from the
    first redemption is in doubt. Killing it turns a stolen code into a denial of
    service against the attacker rather than a second live credential.
    """
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT minted_key_hash FROM oauth_code_used WHERE jti = :j"), {"j": jti}
        ).first()
        if row and row[0]:
            conn.execute(
                text("UPDATE api_key SET revoked_at = now() WHERE key_hash = :h"), {"h": row[0]}
            )


def record_minted_key(jti: str, key_hash: str) -> None:
    """Remember which token a code produced, so reuse can revoke it."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE oauth_code_used SET minted_key_hash = :h WHERE jti = :j"),
            {"h": key_hash, "j": jti},
        )


# --- dynamic client registration store ------------------------------------------


def register_client(redirect_uris: list[str], client_name: str | None) -> str:
    client_id = _CLIENT_PREFIX + secrets.token_urlsafe(16)
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO oauth_client (client_id, redirect_uris, client_name) "
                "VALUES (:id, :u, :n)"
            ),
            {"id": client_id, "u": list(redirect_uris), "n": client_name},
        )
    return client_id


def client_redirect_uris(client_id: str) -> list[str] | None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT redirect_uris FROM oauth_client WHERE client_id = :id"),
            {"id": client_id},
        ).first()
    return list(row[0]) if row is not None else None


def client_name(client_id: str) -> str | None:
    """The registered display name, for the consent screen. None when unnamed."""
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT client_name FROM oauth_client WHERE client_id = :id"),
            {"id": client_id},
        ).first()
    return row[0] if row is not None and row[0] else None
