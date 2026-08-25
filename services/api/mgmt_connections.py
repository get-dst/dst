"""Control-plane warehouse-connection management (/mgmt/connections). Admin-authed.

Customers register a warehouse connection here: non-secret params (`config`) plus an
optional service-account JSON (`secret`), which is encrypted at rest (Fernet). The
secret is never returned by the API.

Creating (or updating) a connection is gated by an evaluation: it must connect + read,
and — when the connection requests write access — it must also pass a write probe. A
failed check blocks the write and reports the exact failing stage. The Test button reruns
the same evaluation against the stored access.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.auth.deps import get_admin_org, get_app_session
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses import store as lens_store
from services.lenses.connection_eval import EvalResult, evaluate_connection, normalize_access
from services.lenses.connection_store import CONTEXT_SOURCE_TYPES
from services.lenses.connections import build_connector, resolve_connector
from services.lenses.profile_enrich_lm import run_description_pass
from services.lenses.profiler import run_sampling_pass
from services.lenses.profiler_catalog import run_catalog_pass
from services.security import crypto

log = logging.getLogger("dst")


def _profile_in_background(org_id: uuid.UUID, name: str) -> None:
    """A connection added/edited gets the full profiling chain in the background: the
    cheap catalog pass, then the guarded sampling pass (enum literals, null rates,
    freshness), then the LLM description pass (F4) — so dst knows what every table
    and column contains without the lens wizard authoring it by hand."""
    try:
        with org_session(org_id) as session:
            connector = resolve_connector(name, org_id)
            run_catalog_pass(session, connector, name)
            run_sampling_pass(session, connector, name)
            run_description_pass(session, name)
    except Exception:
        log.exception("background profile pass failed for connection %s", name)


router = APIRouter(prefix="/mgmt/connections", tags=["connections"])

_ACCESS_MODES = {"read", "write"}


class ConnectionBody(BaseModel):
    name: str
    type: str  # "duckdb" | "bigquery" | "postgres" | "mysql" | "snowflake"
    config: dict[str, Any] = {}
    # SA JSON (bigquery), password (sql), or a PEM private key (snowflake keypair —
    # detected by shape, or forced with config.auth="keypair"). Encrypted at rest,
    # never returned.
    secret: str | None = None
    access: list[str] = Field(default_factory=lambda: ["read"])  # subset of {read, write}


def _require_crypto_if_secret(secret: str | None) -> None:
    if secret and not crypto.is_configured():
        raise HTTPException(
            status_code=503,
            detail="cannot store a credential: DST_SECRET_KEY is not set",
        )


def _validate_secret(type_: str, secret: str | None) -> None:
    if type_ == "bigquery" and secret:
        try:
            info = json.loads(secret)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"secret is not valid JSON: {exc}") from exc
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise HTTPException(
                status_code=400, detail="bigquery secret must be a service-account JSON"
            )


def _validate_access(access: list[str]) -> None:
    unknown = {a.strip().lower() for a in access} - _ACCESS_MODES
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown access mode(s): {', '.join(sorted(unknown))}"
        )


def _evaluate_or_400(
    type_: str, config: dict[str, Any], secret: str | None, access: list[str]
) -> EvalResult:
    """Build a connector from the (unsaved) params and prove the requested access.

    Raises 400 with the exact failing stage if connect/read — or write, when requested —
    does not pass. Returns the EvalResult (incl. table count) on success.
    """
    try:
        connector = build_connector(type_, config, secret)
    except Exception as exc:  # noqa: BLE001 — bad/missing params surface as a 400
        raise HTTPException(status_code=400, detail=f"invalid connection config: {exc}") from exc
    result = evaluate_connection(connector, access)
    if not result.ok:
        failed = result.failure
        detail = (
            f"{failed.stage} access check failed: {failed.error}"
            if failed
            else "connection check failed"
        )
        raise HTTPException(status_code=400, detail=detail)
    return result


@router.get("")
def list_connections(
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """The org's warehouse connections."""
    return [
        c.model_dump()
        for c in connection_store.list_connections(session)
        if c.type not in CONTEXT_SOURCE_TYPES
    ]


@router.post("", status_code=201)
def create_connection(
    body: ConnectionBody,
    background: BackgroundTasks,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Register a warehouse connection (duckdb/bigquery/postgres/mysql/snowflake).
    The credential is probed before anything is stored — a 400 names the failing
    access stage — and a first profile runs in the background. 409 on a duplicate."""
    if body.type not in {"duckdb", "bigquery", "postgres", "mysql", "snowflake"}:
        raise HTTPException(status_code=400, detail=f"unsupported connection type '{body.type}'")
    if connection_store.get_connection(session, body.name) is not None:
        raise HTTPException(status_code=409, detail=f"connection '{body.name}' already exists")
    _require_crypto_if_secret(body.secret)
    _validate_secret(body.type, body.secret)
    _validate_access(body.access)
    access = normalize_access(body.access)
    result = _evaluate_or_400(body.type, body.config, body.secret, access)
    config = {**body.config, "access": access}
    connection_store.create_connection(session, body.name, body.type, config, body.secret)
    background.add_task(_profile_in_background, org_id, body.name)
    return {
        "name": body.name,
        "type": body.type,
        "has_secret": bool(body.secret),
        "access": access,
        "tables": result.tables,
    }


@router.put("/{name}")
def update_connection(
    name: str,
    body: ConnectionBody,
    background: BackgroundTasks,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Replace a connection's config; omit `secret` to keep the stored one. The
    probe runs before saving, and a background re-profile follows."""
    existing = connection_store.get_connection(session, name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"connection '{name}' not found")
    _require_crypto_if_secret(body.secret)
    _validate_secret(body.type, body.secret)
    _validate_access(body.access)
    access = normalize_access(body.access)
    # On update the secret may be omitted (keep existing) — evaluate with whatever the
    # caller supplied, falling back to the stored secret so the probe uses a real credential.
    secret = body.secret if body.secret is not None else connection_store.get_secret(session, name)
    _evaluate_or_400(existing.type, body.config, secret, access)
    config = {**body.config, "access": access}
    connection_store.update_connection(session, name, config, body.secret)
    background.add_task(_profile_in_background, org_id, name)
    return {"name": name, "updated": True, "access": access}


@router.post("/{name}/test")
def test_connection(
    name: str,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> dict[str, object]:
    """Probe a stored connection now — per-stage access checks; reachable tables
    on success, a 400 naming the failing stage otherwise."""
    rec = connection_store.get_connection(session, name)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"connection '{name}' not found")
    access = normalize_access(rec.config.get("access", ["read"]))
    try:
        connector = resolve_connector(name, org_id)
    except Exception as exc:  # noqa: BLE001 — surface any driver/credential error to the caller
        raise HTTPException(status_code=400, detail=f"connection test failed: {exc}") from exc
    result = evaluate_connection(connector, access)
    if not result.ok:
        failed = result.failure
        detail = (
            f"{failed.stage} access check failed: {failed.error}"
            if failed
            else "connection test failed"
        )
        raise HTTPException(status_code=400, detail=detail)
    return {"name": name, "ok": True, "tables": result.tables, "access": access}


@router.get("/{name}/dependents")
def connection_dependents(
    name: str,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Lenses that reference this connection — shown as a delete-time warning so an
    operator never pulls a credential out from under a live lens unknowingly."""
    if connection_store.get_connection(session, name) is None:
        raise HTTPException(status_code=404, detail=f"connection '{name}' not found")
    lenses = lens_store.list_dependent_lenses(session, name)
    return {"name": name, "lenses": [ln.model_dump() for ln in lenses]}


@router.delete("/{name}", status_code=204)
def delete_connection(name: str, session: Session = Depends(get_app_session)) -> None:
    """Delete a connection outright — check `GET …/dependents` first; nothing here
    blocks removing one a live lens still reads through. 404 when unknown."""
    if connection_store.delete_connection(session, name) == 0:
        raise HTTPException(status_code=404, detail=f"connection '{name}' not found")
