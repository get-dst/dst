"""Caller identities + API keys.

Callers are org-scoped (RLS). API keys are looked up by hash before org context
exists, so `api_key` has no RLS and verification uses the admin engine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth.tokens import hash_token, new_caller_key, new_oauth_token
from services.config import settings
from services.db.session import admin_engine


@dataclass
class CallerIdentity:
    org_id: uuid.UUID
    name: str
    is_admin: bool
    caller_id: uuid.UUID | None = None
    groups: list[str] = field(default_factory=list)
    # OAuth/API-key scopes. Empty = unrestricted, which is what every credential
    # minted before scopes existed carries — see services/auth/scopes.py.
    scopes: list[str] = field(default_factory=list)
    # The acting client — an MCP client name, or a direct caller self-identifying via
    # X-dst-Agent. A LABEL for attribution, never a security input: the person is
    # `name` (from the verified credential); this is who is driving them. None on the
    # data plane when nobody said, "mcp" for an MCP request whose client did not name
    # itself — see services/auth/deps.py.
    agent: str | None = None


def create_caller(
    session: Session, name: str, type_: str = "service", groups: list[str] | None = None
) -> uuid.UUID:
    cid = session.execute(
        text(
            "INSERT INTO caller (org_id, name, type, groups) VALUES "
            "(NULLIF(current_setting('app.current_org', true), '')::uuid, :n, :t, :g) RETURNING id"
        ),
        {"n": name, "t": type_, "g": groups or []},
    ).scalar_one()
    return uuid.UUID(str(cid))


def caller_id_by_name(session: Session, name: str) -> uuid.UUID | None:
    row = session.execute(text("SELECT id FROM caller WHERE name = :n"), {"n": name}).first()
    return uuid.UUID(str(row[0])) if row else None


def issue_key(
    session: Session,
    caller_id: uuid.UUID,
    *,
    expires_in_days: int | None = None,
    scopes: list[str] | None = None,
) -> str:
    """Mint a `dst_` service key.

    `expires_in_days` defaults to `DST_TOKEN_DEFAULT_EXPIRY_DAYS` (unset =
    non-expiring, which is what every key issued before this behaved like), and is
    capped by `DST_TOKEN_MAX_EXPIRY_DAYS`. Expiry is the field the whole OSS
    field misses — Metabase API keys have none, dbt service tokens have none,
    DataHub PATs cannot be revoked at all — so having a policy surface here is
    ahead of the benchmark rather than catching up to it.
    """
    raw = new_caller_key()
    days = expires_in_days if expires_in_days is not None else settings.token_default_expiry_days
    cap = settings.token_max_expiry_days
    if cap and (days is None or days > cap):
        # Cap rather than reject: an operator asking for longer than policy allows
        # gets the longest key policy permits, not a failed mint and no key at all.
        days = cap
    session.execute(
        text(
            "INSERT INTO api_key (org_id, caller_id, key_hash, prefix, scopes, expires_at) VALUES "
            "(NULLIF(current_setting('app.current_org', true), '')::uuid, :cid, :h, :p, :s, "
            # Cast explicitly: :d appears twice with no column context, so Postgres
            # cannot infer its type and raises AmbiguousParameter.
            "CASE WHEN CAST(:d AS integer) IS NULL THEN NULL "
            "ELSE now() + make_interval(days => CAST(:d AS integer)) END)"
        ),
        {"cid": caller_id, "h": hash_token(raw), "p": raw[:12], "s": scopes or [], "d": days},
    )
    return raw


# How stale `last_used_at` is allowed to be. api_key is resolved on every REST call
# and every MCP tool invocation; an unconditional UPDATE there turns a read-mostly
# table into a contended one for a column nobody reads in real time.
KEY_TOUCH_INTERVAL = timedelta(minutes=5)


def touch_key(key_hash: str) -> None:
    """Record that a key was used, at most once per KEY_TOUCH_INTERVAL."""
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE api_key SET last_used_at = now() WHERE key_hash = :h "
                "AND (last_used_at IS NULL OR last_used_at < now() - :iv)"
            ),
            {"h": key_hash, "iv": KEY_TOUCH_INTERVAL},
        )


def list_callers(session: Session) -> list[dict[str, object]]:
    rows = session.execute(text("SELECT id, name, type, groups FROM caller ORDER BY name")).all()
    return [{"id": str(r[0]), "name": r[1], "type": r[2], "groups": list(r[3] or [])} for r in rows]


def list_keys(session: Session, caller_name: str) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            "SELECT k.id, k.prefix, k.created_at, k.revoked_at "
            "FROM api_key k JOIN caller c ON c.id = k.caller_id "
            "WHERE c.name = :n "
            "AND k.org_id = NULLIF(current_setting('app.current_org', true), '')::uuid "
            "ORDER BY k.created_at DESC"
        ),
        {"n": caller_name},
    ).all()
    return [
        {
            "id": str(r[0]),
            "prefix": r[1],
            "created_at": r[2].isoformat(),
            "revoked": r[3] is not None,
        }
        for r in rows
    ]


def revoke_key(session: Session, key_id: uuid.UUID) -> int:
    # api_key has no RLS, so scope the revoke to the caller's org explicitly.
    res = session.execute(
        text(
            "UPDATE api_key SET revoked_at = now() "
            "WHERE id = :id AND revoked_at IS NULL "
            "AND org_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
        ),
        {"id": key_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def mint_oauth_token(
    org_id: uuid.UUID,
    caller_id: uuid.UUID,
    ttl: timedelta,
    *,
    scopes: list[str] | None = None,
    resource: str | None = None,
) -> str:
    """Mint an expiring `dsto_` OAuth access token bound to a caller.

    Stored in `api_key` (no RLS — looked up by hash before org context), so it's
    verified by the same `verify_caller_key` path as service keys; the difference is
    `expires_at` (service `dst_` keys leave it NULL = non-expiring). Revocation is
    immediate via `revoke_key` / `revoked_at`, same as any key.

    `scopes` is what the person actually consented to; `resource` is the audience
    the client named (RFC 8707), recorded so a token minted against one base URL
    is not honoured by a second deployment sharing this database.
    """
    raw = new_oauth_token()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_key (org_id, caller_id, key_hash, prefix, expires_at, "
                "scopes, resource) VALUES (:o, :c, :h, :p, now() + :ttl, :s, :r)"
            ),
            {
                "o": org_id,
                "c": caller_id,
                "h": hash_token(raw),
                "p": raw[:12],
                "ttl": ttl,
                "s": scopes or [],
                "r": resource,
            },
        )
    return raw


def verify_caller_key(raw: str, *, resource: str | None = None) -> CallerIdentity | None:
    """Resolve a `dst_`/`dsto_` credential, or None.

    When `resource` is supplied the token's recorded audience must match it. A token
    with no recorded audience passes: every `dst_` service key predates audience
    binding and is issued out-of-band for this deployment, so rejecting them would
    break every existing caller to close a hole they cannot be used for.

    Two steps, not a join: `caller` is FORCE-RLS and on managed Postgres
    (Cloud SQL/RDS/Neon) the admin role is NOT a superuser and has no BYPASSRLS,
    so a join through `caller` with no org context returns nothing and every key
    reads as invalid. `api_key` carries no RLS precisely so the hash lookup can
    resolve the org first; only then is `caller` read, under that org's GUC.
    """
    with admin_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT org_id, caller_id, scopes, resource FROM api_key "
                "WHERE key_hash = :h AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())"
            ),
            {"h": hash_token(raw)},
        ).first()
        if row is None:
            return None
        if resource is not None and row[3] is not None and row[3] != resource:
            return None
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(row[0])})
        caller = conn.execute(
            text("SELECT name, groups FROM caller WHERE id = :c"), {"c": row[1]}
        ).first()
    if caller is None:
        return None
    touch_key(hash_token(raw))
    return CallerIdentity(
        org_id=uuid.UUID(str(row[0])),
        caller_id=uuid.UUID(str(row[1])),
        name=caller[0],
        is_admin=False,
        groups=list(caller[1] or []),
        scopes=list(row[2] or []),
    )
