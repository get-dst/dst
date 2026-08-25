"""request_log: which generation path answered, and what context rode along.

`generator_tier` (certified | intent | grounded) names the PROMPT the answer was
written against — the tiers render different prompts, so a regression that only
shows on one of them was invisible in the log. `repairs` counts the repair
attempts consumed. `context_refs` finally persists a TraceLog field that had
been declared since 0002 and never assigned by anything.
Pre-migration rows stay NULL — honest "unknown", never backfilled.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

COLUMNS = ("generator_tier text", "repairs integer", "context_refs jsonb")


def upgrade() -> None:
    for column in COLUMNS:
        op.execute(f"ALTER TABLE request_log ADD COLUMN IF NOT EXISTS {column}")


def downgrade() -> None:
    for column in COLUMNS:
        op.execute(f"ALTER TABLE request_log DROP COLUMN IF EXISTS {column.split()[0]}")
