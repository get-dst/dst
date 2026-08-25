"""Control-plane (/mgmt) router. Admin-token authenticated.

Lens CRUD, connections, governance, observe and reviews mount under this prefix;
this module itself carries `/mgmt/ping`, which exercises admin auth + org context.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import text

from services.auth.deps import get_admin_org
from services.db.session import admin_engine

router = APIRouter(prefix="/mgmt", tags=["mgmt"])


@router.get("/ping")
async def ping(org_id: uuid.UUID = Depends(get_admin_org)) -> dict[str, str]:
    """Authenticated no-op: confirms the admin token resolved to an org."""
    return {"status": "ok", "org_id": str(org_id)}


@router.get("/whoami")
async def whoami(org_id: uuid.UUID = Depends(get_admin_org)) -> dict[str, str | None]:
    """The org the active token belongs to — lets the UI label the current tenant."""
    with admin_engine.connect() as conn:
        name = conn.execute(text("SELECT name FROM org WHERE id = :i"), {"i": org_id}).scalar()
    return {"org_id": str(org_id), "org_name": name}
