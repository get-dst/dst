"""Add certified_answer.verified_prose — the prose composed ONCE at certify time.

A certified serve used to recompose its English on every
request — ~6s of latency and a badge that wobbled with the composer's mood, on
the one path a human already approved. The prose is now composed once from the
executed result when the answer is certified (`apply --probe-certified`,
`rule --certify`) and served verbatim after that. Nullable text — legacy
answers, no-probe applies and templates (deterministically rendered at serve)
simply leave it null and keep today's behavior.

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS verified_prose text")


def downgrade() -> None:
    op.execute("ALTER TABLE certified_answer DROP COLUMN IF EXISTS verified_prose")
