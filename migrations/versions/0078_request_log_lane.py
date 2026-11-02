"""request_log.lane: the path that served a row apart from the governed lanes.

``explore`` marks an outside-lens read: the `sql` tool inside a lens's scope,
or an answer that read a table the lens exposes without definitions. Observe
counts the lane's cost apart, and `dst test` scores its answers as their own
path. NULL on governed rows and on rows written before the column.

Revision ID: 0078
Revises: 0077
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS lane text")


def downgrade() -> None:
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS lane")
