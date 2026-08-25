"""Org-standard definitions — now a VIEW over the shared semantic layer.

The shared semantic layer subsumed the org_standard table: shared definitions (semantic_asset,
kind='definition') ARE the org's standards. This module keeps the original
OrgStandard API so drift/validate/mgmt call sites are untouched, but reads and
writes shared assets. Org-scoped via RLS as before.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.semantic import store as semantic_store


class OrgStandard(BaseModel):
    term: str
    body: str
    sql_expr: str | None = None


def _to_standard(asset: semantic_store.StoredAsset) -> OrgStandard:
    return OrgStandard(
        term=asset.body.get("term") or asset.name,
        body=asset.body.get("body") or "",
        sql_expr=asset.body.get("sql_expr"),
    )


def upsert_standard(session: Session, term: str, body: str, sql_expr: str | None) -> OrgStandard:
    existing = semantic_store.get_asset(session, "definition", term)
    merged: dict[str, object] = (
        dict(existing.body)
        if existing
        else {"source": "authored", "status": "active", "possible_mappings": []}
    )
    merged.update({"term": term, "body": body, "sql_expr": sql_expr})
    semantic_store.upsert_asset(session, "definition", term, merged)
    return OrgStandard(term=term, body=body, sql_expr=sql_expr)


def list_standards(session: Session) -> list[OrgStandard]:
    return [_to_standard(a) for a in semantic_store.list_assets(session, "definition")]


def get_standard(session: Session, term: str) -> OrgStandard | None:
    asset = semantic_store.get_asset(session, "definition", term)
    return _to_standard(asset) if asset else None


def delete_standard(session: Session, term: str) -> int:
    return semantic_store.delete_asset(session, "definition", term)
