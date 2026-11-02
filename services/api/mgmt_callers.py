"""Control-plane caller + API-key management (/mgmt/callers). Admin-authed."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.auth import oidc_login
from services.auth.deps import get_app_session
from services.governance import credentials

router = APIRouter(prefix="/mgmt/callers", tags=["callers"])


class CallerBody(BaseModel):
    name: str
    type: str = "service"
    groups: list[str] = []


@router.get("")
def list_callers(session: Session = Depends(get_app_session)) -> list[dict[str, object]]:
    """The org's registered callers — the identities `dst_` keys are issued to."""
    return credentials.list_callers(session)


@router.post("", status_code=201)
def create_caller(body: CallerBody, session: Session = Depends(get_app_session)) -> dict[str, str]:
    """Register a caller identity to issue keys against. 409 on a duplicate name."""
    if credentials.caller_id_by_name(session, body.name) is not None:
        raise HTTPException(status_code=409, detail=f"caller '{body.name}' already exists")
    cid = credentials.create_caller(session, body.name, body.type, body.groups)
    return {"id": str(cid), "name": body.name}


class GroupsBody(BaseModel):
    groups: list[str]


@router.put("/{name}/groups")
def set_groups(
    name: str, body: GroupsBody, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """Replace a caller's groups. Admin-only, like every route here: a caller's
    groups decide which lenses and which group-excepted columns it reads, so
    they are never taken from the caller's own requests."""
    groups = sorted({g.strip() for g in body.groups if g.strip()})
    if credentials.set_groups(session, name, groups) == 0:
        raise HTTPException(status_code=404, detail=f"caller '{name}' not found")
    return {"name": name, "groups": groups}


@router.post("/sync-idp")
def sync_idp(
    dry_run: bool = False, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """Deprovisioning against the identity provider: read the configured users
    endpoint (`DST_OIDC_USERS_URL`) and revoke every key of each IdP-bound
    caller whose subject it no longer lists. `dst keys sync-idp` and a scheduled
    `curl` both land here. A listing that cannot be read, or lists nobody,
    revokes nothing and answers 503: a sync that cannot see the IdP must not act
    on its behalf. `dry_run=true` reports without revoking."""
    try:
        return oidc_login.sync_idp(session, dry_run=dry_run)
    except oidc_login.UsersUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class DriverBody(BaseModel):
    driver: bool


@router.put("/{name}/driver")
def set_driver(
    name: str, body: DriverBody, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """Mark a caller a driver: an agent that writes its own Cube queries and
    SQL. On lenses served through Cube dst then calls no model for its
    requests (coverage-only routing, no question read by a model) and runs
    every check on what it submits; typed lenses are unaffected."""
    if credentials.set_driver(session, name, body.driver) == 0:
        raise HTTPException(status_code=404, detail=f"caller '{name}' not found")
    return {"name": name, "driver": body.driver}


@router.post("/{name}/keys", status_code=201)
def issue_key(name: str, session: Session = Depends(get_app_session)) -> dict[str, str]:
    """Mint a `dst_` API key for this caller — the plaintext is returned once, never stored."""
    cid = credentials.caller_id_by_name(session, name)
    if cid is None:
        raise HTTPException(status_code=404, detail=f"caller '{name}' not found")
    raw = credentials.issue_key(session, cid)
    return {"caller": name, "key": raw}  # plaintext shown once


@router.get("/{name}/keys")
def list_keys(name: str, session: Session = Depends(get_app_session)) -> list[dict[str, object]]:
    """The caller's issued keys — metadata only, plaintext appears only at issue time."""
    return credentials.list_keys(session, name)


@router.delete("/{name}/keys/{key_id}", status_code=204)
def revoke_key(name: str, key_id: str, session: Session = Depends(get_app_session)) -> None:
    """Revoke one key by id. 404 when unknown or already revoked."""
    try:
        kid = uuid.UUID(key_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="key not found") from exc
    if credentials.revoke_key(session, kid) == 0:
        raise HTTPException(status_code=404, detail="key not found or already revoked")
