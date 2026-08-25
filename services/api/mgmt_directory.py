"""Control-plane people-directory API (/mgmt/directory). Admin-authed.

Surfaces the org's people plus the access "groups" (everyone / per-department /
reports-to) used to grant lens access. The group ids returned here match the caller
groups minted by `services.governance.directory.seed_people`, so a lens allow-list
built from them works with `policy.authorize` unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from services.auth.deps import get_app_session
from services.governance import directory

router = APIRouter(prefix="/mgmt/directory", tags=["directory"])


@router.get("")
def get_directory(session: Session = Depends(get_app_session)) -> dict[str, Any]:
    """The org's people plus the derived access groups (`everyone`, `dept:<name>`,
    `reports-to:<id>`) — the exact ids lens allow-lists are built from."""
    people = directory.list_people(session)

    # Distinct, sorted department names (skip blanks).
    departments = sorted({p["department"] for p in people if p.get("department")})

    # Managers: anyone referenced by another person's manager_id. The manager id here is
    # the external manager_id value (employee_id / rep_id), matching the reports-to group
    # encoding. The display name is resolved via the manager_name carried on each report.
    managers_by_id: dict[str, str] = {}
    for p in people:
        mid = p.get("manager_id")
        if mid and mid not in managers_by_id:
            managers_by_id[mid] = p.get("manager_name") or mid
    managers = [{"id": mid, "name": name} for mid, name in managers_by_id.items()]
    managers.sort(key=lambda m: m["name"])

    groups: list[dict[str, str]] = [{"id": "everyone", "label": "Everyone", "kind": "all"}]
    groups += [
        {"id": f"dept:{d}", "label": f"Everyone in {d}", "kind": "department"} for d in departments
    ]
    groups += [
        {
            "id": f"reports-to:{m['id']}",
            "label": f"Reports to {m['name']}",
            "kind": "reports_to",
        }
        for m in managers
    ]

    return {
        "people": people,
        "departments": departments,
        "managers": managers,
        "groups": groups,
    }
