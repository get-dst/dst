"""eval_case.tags — free-form classification for eval cases.

A persona-driven battery needs each case to say WHO it is for (persona:cfo) and
WHY it exists (intent:discriminator) — the
aggregate score hides exactly the slices that carry signal (routing accuracy on
discriminator cases, refuse-rate on out-of-scope cases). The vocabulary is a
project convention; dst owns only the slot. text[] over jsonb: a flat list,
GIN-indexable if tag filtering ever needs it. No data rewrite — unlike 0031
there is no legacy encoding to lift, because the key was never accepted.

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE eval_case ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE eval_case DROP COLUMN IF EXISTS tags")
