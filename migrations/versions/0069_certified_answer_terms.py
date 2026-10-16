"""certified_answer.terms — what a certified answer's SQL reads, by name.

Attribution runs over every certified answer's SQL at apply and stores the
governed names it reads (metric and definition names) and the columns it
aggregates. The serve-time lexical veto reads them:
an answer whose question and SQL carry no governed name at all is compared
word by word against the asked question, so a raw collections template never
serves an invoicing question. Nullable: rows applied before this column exist
are read without it until their next apply backfills them.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS terms jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS terms")
