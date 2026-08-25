"""person: org-scoped people directory for the access step

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE person (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         email text,
         department text,
         title text,
         manager_id text,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE person ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE person FORCE ROW LEVEL SECURITY",
    """CREATE POLICY person_isolation ON person
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON person TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS person"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
