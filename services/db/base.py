"""Declarative base + the standard RLS policy used by every tenant table."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def rls_policy_statements(table: str) -> list[str]:
    """SQL to make `table` tenant-isolated by `org_id`.

    Reads the per-transaction GUC `app.current_org` (set by services.db.session).
    FORCE is required so the table owner is also subject to the policy; the app
    connects as a NON-superuser role so the policy is enforced (superusers bypass RLS).
    Fail-closed: when the GUC is unset, `current_setting(..., true)` is NULL and no
    rows match.
    """
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        (
            f"CREATE POLICY {table}_isolation ON {table} "
            "USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid) "
            "WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)"
        ),
    ]
