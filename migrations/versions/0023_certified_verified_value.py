"""Add certified_answer.verified_value — the value the generated SQL produced.

Generated certified answers (lens-ux: certified Q→SQL pipeline) execute their SQL
read-only at generation time and capture what it returned, so the Certified tab can
show a verified value alongside each question→SQL pair and the lens can be graded
against it. Nullable JSONB — hand-certified answers (no execution) simply leave it null.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS verified_value jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS verified_value")
