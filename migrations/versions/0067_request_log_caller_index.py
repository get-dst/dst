"""Index request_log by (org, caller, created_at) for the daily quota.

The per-caller daily quota counts a caller's governed answers over the last 24
hours on every request. The only index so far led with lens, which the quota
does not filter on; without this one the count is a scan of a table that only
ever grows.

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_request_log_org_caller_created "
        "ON request_log (org_id, caller, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_request_log_org_caller_created")
