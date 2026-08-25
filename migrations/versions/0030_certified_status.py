"""certified_answer.status — active | retired.

Retirement is explicit and keeps history: a retired answer is never served,
never matched, and never tested by the certified suite — but it still lists
and exports (certified_answers.yaml renders the key only when retired).
Existing answers are active by definition, hence the default.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active'",
]

DOWNGRADE = [
    "ALTER TABLE certified_answer DROP COLUMN IF EXISTS status",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
