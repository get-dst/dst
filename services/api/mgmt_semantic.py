"""Control-plane shared semantic layer (/mgmt/semantic). Admin-authed, org-scoped.

Asset CRUD over the semantic_asset store, plus the AI drafter surface:
introspection + stored table profiles + a use-case prompt → SharedEntity files
and glossary pages, returned as semantic/** files (review = git diff, landing =
plan/apply) or upserted directly with ?persist=true (source="draft").
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from services.auth.deps import get_admin_org, get_app_session
from services.contracts.protocols import TargetedIntrospect
from services.contracts.semantic_model import Definition
from services.contracts.shared_semantic import SharedEntity
from services.lenses import profile_store
from services.lenses.connections import resolve_connector
from services.semantic import store
from services.semantic.introspect import empty_listing_reason, schema_json, serialize_schema

router = APIRouter(prefix="/mgmt/semantic", tags=["semantic"])


class DraftBody(BaseModel):
    connection: str
    prompt: str
    tables: list[str] | None = None


@router.get("")
def list_assets(
    kind: store.AssetKind | None = None, session: Session = Depends(get_app_session)
) -> list[store.StoredAsset]:
    """The shared semantic layer's stored assets — entities and definitions,
    optionally filtered to one kind."""
    return store.list_assets(session, kind)


@router.get("/{kind}/{name}")
def get_asset(
    kind: store.AssetKind, name: str, session: Session = Depends(get_app_session)
) -> store.StoredAsset:
    """One published asset, body included — the read-back `dst semantic get`
    wraps. Without it, confirming that a declaration landed means a direct
    Postgres query, since the only other read surface is the unfiltered list."""
    asset = store.get_asset(session, kind, name)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"{kind} '{name}' not found")
    return asset


@router.put("/{kind}/{name}")
def upsert_asset(
    kind: store.AssetKind,
    name: str,
    body: dict[str, object],
    session: Session = Depends(get_app_session),
) -> store.StoredAsset:
    """Upsert one shared asset; the body is the asset body (entity YAML shape /
    definition fields). The name in the path wins over any name in the body."""
    try:
        model: SharedEntity | Definition
        if kind == "entity":
            model = SharedEntity.model_validate({**body, "name": name})
        else:
            model = Definition.model_validate({**body, "term": name})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return store.upsert_asset(session, kind, name, model.model_dump(mode="json"))


@router.delete("/{kind}/{name}", status_code=204)
def delete_asset(
    kind: store.AssetKind, name: str, session: Session = Depends(get_app_session)
) -> None:
    """Delete a shared asset. 409 while a published lens still selects it; 404 when absent."""
    try:
        deleted = store.delete_asset(session, kind, name)
    except ValueError as exc:  # a published lens still selects it
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"{kind} '{name}' not found")


@router.get("/introspect")
def introspect_connection(
    connection: str,
    tables: str | None = None,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> dict[str, object]:
    """The raw material for authoring semantic/ files: schema + stored profile
    facts (row counts, null rates, enum values, ranges), agent-legible in
    ``text`` and parseable in ``json``. This is the answer to drafting —
    point your own agent at it. Facts here are whatever the connection's
    profiling passes stored; `dst introspect --profile` samples on demand."""
    try:
        connector = resolve_connector(connection, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown connection: {exc}") from exc
    table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
    try:
        # A named subset resolves against the FULL catalog where the connector
        # can — the capped listing must never be what --tables matches against.
        if table_list and isinstance(connector, TargetedIntrospect):
            snapshot = connector.introspect_tables(table_list)
        else:
            snapshot = connector.introspect()
    except Exception as exc:  # noqa: BLE001 — dead creds must be actionable, not a bare 500
        raise HTTPException(
            status_code=502,
            detail=f"connection '{connection}' failed to introspect: {exc} — fix its "
            "credentials/config (dst.yaml + the secret env ref) and re-apply",
        ) from exc
    profiles = [p.profile for p in profile_store.list_profiles(session, connection)]
    reason = empty_listing_reason(connection, snapshot, table_list)
    if reason is not None:
        # An empty 200 is the failure shape this endpoint must never have.
        raise HTTPException(status_code=404, detail=reason)
    return {
        "text": serialize_schema(snapshot, profiles, table_list),
        "json": schema_json(snapshot, profiles, table_list),
    }
