"""certified_answer.resolution — the typed gold a certified answer carries.

When a served answer is certified, the trace's resolution ledger (which metric
/ definition / dimensions / grain / filters the figure resolved to, each with
its source) is copied onto the certified row. `dst test` grades a question's
typed resolution against it BEFORE any SQL runs — the slot lane — and the rows
lane keeps grading values after. Nullable: answers certified before the ledger
existed grade against their attributed oracle SQL as before, and never pretend
to carry gold they do not have.

Revision ID: 0065
Revises: 0064
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS resolution jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS resolution")
