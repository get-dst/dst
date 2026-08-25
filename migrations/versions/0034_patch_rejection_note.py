"""patch_candidate.rejection_note — why a human declined a drafted fix.

The queue had approve-or-limbo: a mistargeted draft sat as 'candidate' forever
and the rejection reason — the most valuable feedback the drafter could get —
had nowhere to live. Nullable: approvals and still-pending candidates carry none.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patch_candidate ADD COLUMN IF NOT EXISTS rejection_note text")


def downgrade() -> None:
    op.execute("ALTER TABLE patch_candidate DROP COLUMN IF EXISTS rejection_note")
