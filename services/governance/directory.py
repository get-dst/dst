"""People directory for the access step. Org-scoped via RLS.

A `person` row is a lightweight org-member record (name, email, department, title,
reporting line). `seed_people` also mints a `caller` per person whose `groups`
encode org membership, department, and reporting line so that `policy.authorize`
can grant access by group without any extra wiring. What to seed is the caller's
business — this module owns only the mechanism.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.governance import credentials


def upsert_person(
    session: Session,
    *,
    name: str,
    email: str | None,
    department: str | None,
    title: str | None,
    manager_id: str | None,
) -> str:
    """Insert a person for the current org. Org comes from the current_org GUC."""
    pid = session.execute(
        text(
            """
            INSERT INTO person (org_id, name, email, department, title, manager_id)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :name, :email, :department, :title, :manager_id
            )
            RETURNING id
            """
        ),
        {
            "name": name,
            "email": email,
            "department": department,
            "title": title,
            "manager_id": manager_id,
        },
    ).scalar_one()
    return str(pid)


def list_people(session: Session) -> list[dict[str, Any]]:
    """Return all people for the current org, each with a resolved `manager_name`.

    `manager_id` holds the manager's external id (employee_id / rep_id), the same value
    used by the `reports-to:<manager_id>` caller group. The person table stores no
    internal FK, so the manager's name is resolved in Python against an external-id ->
    name index built from each manager's own external id (see `_external_index`).
    """
    rows = session.execute(
        text("SELECT id, name, email, department, title, manager_id FROM person ORDER BY name")
    ).all()
    index = _external_index(session)
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "email": r[2],
            "department": r[3],
            "title": r[4],
            "manager_id": r[5],
            "manager_name": index.get(r[5]) if r[5] else None,
        }
        for r in rows
    ]


def _external_index(session: Session) -> dict[str, str]:
    """Map each person's external id (employee_id / rep_id) -> their name.

    A manager is referenced by `manager_id`, the manager's external id. Each seeded
    caller carries an internal ``self:<external_id>`` group and is named with the
    person's email, so joining caller -> person (by email) and reading the ``self:``
    group yields external-id -> name. The ``self:`` group is internal only and is never
    emitted by the directory API, so it does not affect group-based authorization.
    """
    rows = session.execute(
        text(
            "SELECT c.groups, p.name FROM person p "
            "JOIN caller c ON c.name = p.email WHERE c.type = 'user'"
        )
    ).all()
    index: dict[str, str] = {}
    for groups, name in rows:
        for g in groups or []:
            if isinstance(g, str) and g.startswith("self:"):
                index[g[len("self:") :]] = name
    return index


def _make_caller(
    session: Session,
    *,
    handle: str,
    external_id: str | None,
    department: str | None,
    manager_id: str | None,
) -> None:
    """Create a caller for a person; tolerate pre-existing callers on re-seed.

    Groups are exactly ["everyone", "dept:<department>", "reports-to:<manager_id>"]
    (the latter two only when present) — these match the directory API's group ids so
    `policy.authorize` works unchanged. An internal "self:<external_id>" group is added
    for manager-name resolution; it is never surfaced by the directory API.
    """
    groups = ["everyone"]
    if department:
        groups.append(f"dept:{department}")
    if manager_id:
        groups.append(f"reports-to:{manager_id}")
    if external_id:
        groups.append(f"self:{external_id}")
    # Wrap in a SAVEPOINT so a duplicate-name caller (existing from a prior seed) only
    # rolls back this insert, not the whole re-seed transaction.
    try:
        with session.begin_nested():
            credentials.create_caller(session, name=handle, type_="user", groups=groups)
    except Exception:
        pass  # a caller with this name may already exist on re-seed


def seed_people(session: Session, people: list[dict[str, Any]]) -> dict[str, int]:
    """Replace the org's directory with `people`, minting a caller per person.

    Each entry carries name/email/department/title/manager_id plus `external_id`
    (employee/rep id, becomes the internal ``self:`` group) and `handle` (the
    caller's name — conventionally the email). Idempotent: clears existing org
    people first (RLS scopes the delete to the org). Each caller's groups are
    ["everyone", "dept:<department>", and "reports-to:<manager_id>" when a
    manager exists] — the ids `policy.authorize` grants by.
    """
    session.execute(text("DELETE FROM person"))
    count = 0
    for p in people:
        upsert_person(
            session,
            name=p["name"],
            email=p["email"],
            department=p["department"],
            title=p["title"],
            manager_id=p["manager_id"],
        )
        _make_caller(
            session,
            handle=p["handle"],
            external_id=p["external_id"],
            department=p["department"],
            manager_id=p["manager_id"],
        )
        count += 1

    return {"people": count}
