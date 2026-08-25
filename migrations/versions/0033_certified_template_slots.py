"""certified_answer.slots / .sample_bindings — parameterized certified answers.

Two nullable jsonb columns: ``slots`` types each {placeholder} in a template's
SQL/question (services/certify/binding.py owns the types and validators);
``sample_bindings`` are the template's executable witnesses — [0] is the match
anchor and the eval oracle's binding. A row with neither is exactly a
pre-PC frozen pair; nothing is backfilled.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS slots jsonb")
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS sample_bindings jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS sample_bindings")
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS slots")
