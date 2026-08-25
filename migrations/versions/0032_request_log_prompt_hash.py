"""request_log.prompt_hash — which serving prompt-set produced each trace.

Nullable text column stamped at persist time from runtime.prompt_version, so
an eval trend or a production regression can be attributed to a prompt edit.
Pre-migration rows stay NULL — honest "unknown", never backfilled.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_log ADD COLUMN IF NOT EXISTS prompt_hash text")


def downgrade() -> None:
    op.execute("ALTER TABLE request_log DROP COLUMN IF EXISTS prompt_hash")
