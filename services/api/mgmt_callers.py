"""Control-plane caller + API-key management (/mgmt/callers). Admin-authed."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
