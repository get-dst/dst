"""request_log.mode and eval_result.mode: the failure mode, one name per row.

A refusal, a clarification or a capped answer carries one of twenty names
(services/contracts/failure_modes.py) saying why it was not a clean answer
and which file fixes it. Stored denormalised on the request log so observe
counts by mode in SQL, and on eval results so a `dst test` run's scorecard
by mode is a query over the same rows the dashboard reads. Nullable: a clean
answer has none, and rows written before this revision stay as they are.

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS mode text")
    op.execute("ALTER TABLE eval_result ADD COLUMN IF NOT EXISTS mode text")


def downgrade() -> None:
    op.execute("ALTER TABLE eval_result DROP COLUMN IF EXISTS mode")
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS mode")
