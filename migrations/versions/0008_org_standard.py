"""org_standard: org-wide canonical definitions (drift baseline)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE org_standard (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         term text NOT NULL,
         body text NOT NULL,
         sql_expr text,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "CREATE UNIQUE INDEX ux_org_standard_org_term ON org_standard (org_id, term)",
    "ALTER TABLE org_standard ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE org_standard FORCE ROW LEVEL SECURITY",
    """CREATE POLICY org_standard_isolation ON org_standard
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON org_standard TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS org_standard"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
