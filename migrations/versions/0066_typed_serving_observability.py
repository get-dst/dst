"""Typed serving on the audit: request_log.typed / clarification, eval_run.calibration.

`typed` denormalises Resolution.typed — every slot a typed decision, a
deterministic parse or a caller-supplied binding, no escalation — so the audit
statement can count the typed share in SQL (NULL = served before typed serving,
reported as unknown, never as untyped). `clarification` persists the
clarification a non-answer asked for (kind + slot), so the columns that keep
clarifying for want of a complete value dictionary are a query — profiling
completeness is the lever. `eval_run.calibration` stores the deciders'
calibration table a `dst test` sweep computed, so the accuracy tab shows it.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS typed boolean")
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS clarification jsonb")
    op.execute("ALTER TABLE eval_run ADD COLUMN IF NOT EXISTS calibration jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE eval_run DROP COLUMN IF EXISTS calibration")
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS clarification")
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS typed")
