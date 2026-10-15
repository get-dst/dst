"""request_log.purpose — the caller's one-sentence purpose, when the lens logs it.

A question says what; `purpose` says why ("building a forecasting app"). It is
self-reported prose from the calling agent, stored only when the lens sets
`logging.log_purpose`, so the column also measures how often agents send one.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-15
"""

from __future__ import annotations

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS purpose text")


def downgrade() -> None:
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS purpose")
