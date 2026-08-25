"""request_log: which governance capabilities could NOT run for an answer.

`degraded` persists TraceLog.degraded — today, "certified matching did not run"
when the install has no usable embedder. The live failure it exists for: a reaped
fastembed model cache took certified matching down for a whole session, and every
answer served looked like an ordinary generated one, in the response AND in the
log. With this column "how many answers did we serve while matching was down?" is
a query. Pre-migration rows stay NULL — honest "unknown", never backfilled.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS degraded jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS degraded")
