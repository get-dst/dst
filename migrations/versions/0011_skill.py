"""skill: reusable knowledge packs (instructions + definitions + sample queries)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE skill (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         display_name text NOT NULL,
         description text NOT NULL DEFAULT '',
         instructions text NOT NULL DEFAULT '',
         definitions jsonb NOT NULL DEFAULT '[]'::jsonb,
         sample_queries jsonb NOT NULL DEFAULT '[]'::jsonb,
         version integer NOT NULL DEFAULT 1,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "CREATE UNIQUE INDEX ux_skill_org_name ON skill (org_id, name)",
    "ALTER TABLE skill ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE skill FORCE ROW LEVEL SECURITY",
    """CREATE POLICY skill_isolation ON skill
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON skill TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS skill"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
