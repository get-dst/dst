"""DB-backed warehouse connections with encrypted credentials. Org-scoped via RLS.

A connection row pairs non-secret params (`config`) with an optional Fernet-encrypted
secret (the warehouse SA JSON). `resolve_connector` reads these to build a live
`Connector`; the secret is decrypted only at that moment.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.security import crypto

# Legacy row filter: the in-product context-source connectors (removed — a driver
# agent supplies context now) stored rows in this same table under these types.
# Installs may still carry such rows; warehouse listings must skip them.


class ConnectionRecord(BaseModel):
    name: str
    type: str  # "duckdb" | "bigquery"
    config: dict[str, Any] = {}
    has_secret: bool = False


def create_connection(
    session: Session,
    name: str,
    type_: str,
    config: dict[str, Any],
    secret: str | None,
) -> str:
    enc = crypto.encrypt(secret) if secret else None
    session.execute(
        text(
            """
            INSERT INTO connection (org_id, name, type, config, secret_encrypted)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :name, :type, CAST(:config AS jsonb), :secret
            )
            """
        ),
        {"name": name, "type": type_, "config": _dumps(config), "secret": enc},
    )
    return name


def update_connection(
    session: Session,
    name: str,
    config: dict[str, Any],
    secret: str | None,
) -> int:
    # Only overwrite the secret when a new one is supplied.
    if secret is not None:
        res = session.execute(
            text(
                "UPDATE connection SET config = CAST(:config AS jsonb), "
                "secret_encrypted = :secret, updated_at = now() WHERE name = :name"
            ),
            {"config": _dumps(config), "secret": crypto.encrypt(secret), "name": name},
        )
    else:
        res = session.execute(
            text(
                "UPDATE connection SET config = CAST(:config AS jsonb), "
                "updated_at = now() WHERE name = :name"
            ),
            {"config": _dumps(config), "name": name},
        )
    return int(res.rowcount)  # type: ignore[attr-defined]


def list_connections(session: Session) -> list[ConnectionRecord]:
    rows = session.execute(
        text(
            "SELECT name, type, config, secret_encrypted IS NOT NULL FROM connection ORDER BY name"
        )
    ).all()
    return [
        ConnectionRecord(name=r[0], type=r[1], config=r[2] or {}, has_secret=bool(r[3]))
        for r in rows
    ]


def get_connection(session: Session, name: str) -> ConnectionRecord | None:
    row = session.execute(
        text(
            "SELECT name, type, config, secret_encrypted IS NOT NULL "
            "FROM connection WHERE name = :name"
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    return ConnectionRecord(name=row[0], type=row[1], config=row[2] or {}, has_secret=bool(row[3]))


def get_secret(session: Session, name: str) -> str | None:
    """Decrypt and return the stored secret for `name`, or None."""
    row = session.execute(
        text("SELECT secret_encrypted FROM connection WHERE name = :name"),
        {"name": name},
    ).first()
    if row is None or row[0] is None:
        return None
    return crypto.decrypt(row[0])


def delete_connection(session: Session, name: str) -> int:
    res = session.execute(text("DELETE FROM connection WHERE name = :name"), {"name": name})
    return int(res.rowcount)  # type: ignore[attr-defined]


def _dumps(config: dict[str, Any]) -> str:
    import json

    return json.dumps(config)
