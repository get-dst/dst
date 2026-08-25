"""Control-plane org-standard definitions (/mgmt/standards). Admin-authed.

Org-wide canonical definitions used as the drift baseline during lens validation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.auth.deps import get_app_session
from services.definitions import standards

router = APIRouter(prefix="/mgmt/standards", tags=["standards"])


class StandardBody(BaseModel):
    term: str
    body: str
    sql_expr: str | None = None


@router.get("")
def list_standards(session: Session = Depends(get_app_session)) -> list[standards.OrgStandard]:
    """Every org-standard definition — the baseline lens drift checks compare against."""
    return standards.list_standards(session)


@router.put("/{term}")
def upsert_standard(
    term: str, body: StandardBody, session: Session = Depends(get_app_session)
) -> standards.OrgStandard:
    """Create or replace the org-wide meaning of a term, optionally with its SQL expression."""
    return standards.upsert_standard(session, term, body.body, body.sql_expr)


@router.delete("/{term}", status_code=204)
def delete_standard(term: str, session: Session = Depends(get_app_session)) -> None:
    """Drop a term's org standard. 404 when it never existed."""
    if standards.delete_standard(session, term) == 0:
        raise HTTPException(status_code=404, detail=f"standard '{term}' not found")
