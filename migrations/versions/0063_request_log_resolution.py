"""request_log: the resolution ledger and its derived tag.

`resolution` persists TraceLog.resolution — per slot, where the answer's metric /
definition / grain / dimensions / filters / window came from (certified |
declared | inferred | unknown). `resolution_tag` denormalises the answer-level
tag so the audit statement can count "% of answers whose figure is backed by a
certified or declared definition" in SQL. Pre-migration rows stay NULL — read as
`unknown` by observe, said out loud beside the percentage, never backfilled and
never counted as "not governed".

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS resolution jsonb")
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS resolution_tag text")


def downgrade() -> None:
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS resolution_tag")
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS resolution")
