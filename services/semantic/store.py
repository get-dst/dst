"""Shared semantic assets — org-scoped storage for entities and definitions.

One row per asset (kind 'entity' | 'definition'), body as the pydantic dump,
content_hash for staleness detection (compile provenance compares against it).
RLS org-scoped like siblings; sessions come from org_session().
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.shared_semantic import asset_content_hash

AssetKind = Literal["entity", "definition"]


@dataclass(frozen=True)
class StoredAsset:
    kind: AssetKind
    name: str
    body: dict[str, Any]
    content_hash: str
    source: str


def upsert_asset(
    session: Session,
    kind: AssetKind,
    name: str,
    body: dict[str, Any],
    *,
    source: str = "authored",
) -> StoredAsset:
    digest = asset_content_hash(kind, body)
    session.execute(
        text(
            """
            INSERT INTO semantic_asset (org_id, kind, name, body, content_hash, source)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :kind, :name, CAST(:body AS jsonb), :hash, :source
            )
            ON CONFLICT (org_id, kind, name)
            DO UPDATE SET body = EXCLUDED.body, content_hash = EXCLUDED.content_hash,
                          source = EXCLUDED.source, updated_at = now()
            """
        ),
        {"kind": kind, "name": name, "body": json.dumps(body), "hash": digest, "source": source},
    )
    return StoredAsset(kind=kind, name=name, body=body, content_hash=digest, source=source)


def list_assets(session: Session, kind: AssetKind | None = None) -> list[StoredAsset]:
    where = "WHERE kind = :kind" if kind else ""
    rows = session.execute(
        text(
            f"SELECT kind, name, body, content_hash, source FROM semantic_asset {where} "
            "ORDER BY kind, name"
        ),
        {"kind": kind} if kind else {},
    ).all()
    return [
        StoredAsset(kind=r[0], name=r[1], body=r[2], content_hash=r[3], source=r[4]) for r in rows
    ]


def get_asset(session: Session, kind: AssetKind, name: str) -> StoredAsset | None:
    row = session.execute(
        text(
            "SELECT kind, name, body, content_hash, source FROM semantic_asset "
            "WHERE kind = :kind AND name = :name"
        ),
        {"kind": kind, "name": name},
    ).first()
    if row is None:
        return None
    return StoredAsset(kind=row[0], name=row[1], body=row[2], content_hash=row[3], source=row[4])


def dependent_lenses(session: Session, kind: AssetKind, name: str) -> list[str]:
    """Published lenses whose select references this asset (by lens.yaml select spec)."""
    rows = session.execute(
        text("SELECT name, published_json FROM lens WHERE published_json IS NOT NULL")
    ).all()
    out: list[str] = []
    for lens_name, bundle in rows:
        select = ((bundle or {}).get("config") or {}).get("select") or {}
        if kind == "entity":
            picks = {e.get("name") for e in select.get("entities") or [] if isinstance(e, dict)}
        else:
            picks = set(select.get("definitions") or [])
        if name in picks or "*" in picks:
            out.append(lens_name)
    return out


def delete_asset(session: Session, kind: AssetKind, name: str) -> int:
    """Delete an asset; refuses (ValueError) while a published lens selects it."""
    dependents = dependent_lenses(session, kind, name)
    if dependents:
        raise ValueError(
            f"{kind} '{name}' is selected by published lens(es): {', '.join(sorted(dependents))}"
            " — deselect it there (edit lens.yaml + apply) first"
        )
    res = session.execute(
        text("DELETE FROM semantic_asset WHERE kind = :kind AND name = :name"),
        {"kind": kind, "name": name},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def asset_hashes(session: Session) -> dict[str, str]:
    """{"entity/orders": hash, ...} — the staleness comparison surface."""
    rows = session.execute(text("SELECT kind, name, content_hash FROM semantic_asset")).all()
    return {f"{r[0]}/{r[1]}": r[2] for r in rows}
