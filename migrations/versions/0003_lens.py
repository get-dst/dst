"""lens: DB-backed lens config + semantic model, draft/published

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE lens (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         display_name text NOT NULL,
         description text NOT NULL DEFAULT '',
         status text NOT NULL DEFAULT 'draft',
         draft_json jsonb NOT NULL,
         published_json jsonb,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         published_at timestamptz,
         CONSTRAINT uq_lens_org_name UNIQUE (org_id, name)
       )""",
    "ALTER TABLE lens ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE lens FORCE ROW LEVEL SECURITY",
    """CREATE POLICY lens_isolation ON lens
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON lens TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS lens"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
