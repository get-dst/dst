"""review.origin — who raised the ticket: 'human' (caller/dashboard) or 'ai'
(auto-flagged by the lens's auto_review policy). A triage dimension for the
queue. Existing tickets are human-raised by definition, hence the default.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE review ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'human'",
]

DOWNGRADE = [
    "ALTER TABLE review DROP COLUMN IF EXISTS origin",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
