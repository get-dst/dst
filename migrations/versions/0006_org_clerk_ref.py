"""org.clerk_ref — map a Clerk org/user to a dst org

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE org ADD COLUMN clerk_ref text",
    "CREATE UNIQUE INDEX uq_org_clerk_ref ON org (clerk_ref)",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS uq_org_clerk_ref",
    "ALTER TABLE org DROP COLUMN IF EXISTS clerk_ref",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
