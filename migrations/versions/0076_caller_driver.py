"""caller.driver: the caller is an agent that does its own reading.

A driver caller (an agent writing its own Cube queries and SQL) gets no model
call from dst: routing is coverage only, the question doors do not read its
questions with a model, and every check still runs on what it submits. Set
by an admin; requests may also mark themselves with ``X-DST-Driver: 1``.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE caller ADD COLUMN IF NOT EXISTS driver boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE caller DROP COLUMN IF EXISTS driver")
