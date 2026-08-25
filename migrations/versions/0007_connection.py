"""connection: per-org warehouse connections with encrypted credentials

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE connection (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         type text NOT NULL,
         config jsonb NOT NULL DEFAULT '{}'::jsonb,
         secret_encrypted text,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "CREATE UNIQUE INDEX ux_connection_org_name ON connection (org_id, name)",
    "ALTER TABLE connection ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE connection FORCE ROW LEVEL SECURITY",
    """CREATE POLICY connection_isolation ON connection
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON connection TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS connection"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
