"""routing_decision: the decider's measured decision and the policy verdict.

`decision` persists what the coverage decider measured for a route — provider,
chosen lens, p(chosen), runner-up, margin — and the verdict the threshold policy
gave it (act | clarify | decline). Cosine paths and outages store NULL: nothing
was measured there, and NULL says so. Calibration and the act/clarify split
become queries over this column instead of archaeology.

Revision ID: 0064
Revises: 0063
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE routing_decision ADD COLUMN IF NOT EXISTS decision jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE routing_decision DROP COLUMN IF EXISTS decision")
