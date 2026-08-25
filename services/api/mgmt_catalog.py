"""Catalog endpoints for the lens wizard: available connections + their tables.

Read-only, admin-authed. Lets the creation wizard show connection tiles and a
table picker — served from the stored table profile when fresh,
introspected live (and persisted as a profile) on miss or staleness. Kept
separate from mgmt_connections so it can evolve independently.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from services.auth.deps import get_admin_org, get_app_session
from services.config import settings
from services.contracts.profile import profiles_from_snapshot
from services.lenses import connection_store, profile_store
from services.lenses.connection_store import CONTEXT_SOURCE_TYPES
from services.lenses.connections import resolve_connector

router = APIRouter(prefix="/mgmt/connections", tags=["catalog"])

# Catalog-pass profiles are considered current for a day.
PROFILE_TTL_HOURS = 24.0


def _builtins() -> list[dict[str, object]]:
    out: list[dict[str, object]] = [
        {"name": "jaffle", "type": "duckdb", "builtin": True, "detail": "local sample warehouse"}
    ]
    if settings.gcp_credentials:
        out.append(
            {
                "name": "bigquery",
                "type": "bigquery",
                "builtin": True,
                "detail": settings.bigquery_dataset or "BigQuery",
            }
        )
    return out


@router.get("/available")
def available_connections(
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """Built-in warehouses + the org's registered connections (for the wizard tiles)."""
    registered: list[dict[str, object]] = []
    for c in connection_store.list_connections(session):
        if c.type in CONTEXT_SOURCE_TYPES:
            continue  # context sources (notion/github/…) are NOT data sources
        detail = (
            c.config.get("dataset") or c.config.get("database") or c.config.get("account") or ""
        )
        registered.append({"name": c.name, "type": c.type, "builtin": False, "detail": detail})
    return _builtins() + registered


@router.get("/{name}/tables")
def connection_tables(
    name: str,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Tables for a connection (the wizard table picker).

    Served from the stored profile when every table's profile is within TTL;
    otherwise introspects live, persists the result as fresh profiles, and serves
    that. The response shape is unchanged either way.
    """
    stored = profile_store.list_profiles(session, name)
    if stored and not any(
        profile_store.is_stale(s.profiled_at, ttl_hours=PROFILE_TTL_HOURS) for s in stored
    ):
        return {
            "connection": name,
            "tables": [
                {
                    "name": s.profile.table,
                    "rows": s.profile.row_count,
                    "columns": len(s.profile.columns),
                }
                for s in stored
            ],
        }
    try:
        connector = resolve_connector(name, org_id)
        snapshot = connector.introspect()
    except Exception as exc:  # noqa: BLE001 — surface driver/credential errors to the wizard
        raise HTTPException(status_code=400, detail=f"could not list tables: {exc}") from exc
    for profile in profiles_from_snapshot(snapshot, connection=name):
        profile_store.upsert_profile(session, profile)
    profile_store.prune_profiles(session, name, [t.name for t in snapshot.tables])
    return {
        "connection": name,
        "tables": [
            {"name": t.name, "rows": t.row_count, "columns": len(t.columns)}
            for t in snapshot.tables
        ],
    }
