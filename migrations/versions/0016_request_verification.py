"""request_log.verification + certification — persist the graded trust signal

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE request_log ADD COLUMN verification jsonb",
    "ALTER TABLE request_log ADD COLUMN certification text",
]

DOWNGRADE = [
    "ALTER TABLE request_log DROP COLUMN IF EXISTS certification",
    "ALTER TABLE request_log DROP COLUMN IF EXISTS verification",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
