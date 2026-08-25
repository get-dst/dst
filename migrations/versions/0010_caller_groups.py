"""caller.groups: group/role membership for group-based lens allow-lists

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE caller ADD COLUMN groups text[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE caller DROP COLUMN IF EXISTS groups")
