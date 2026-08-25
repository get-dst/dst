"""api_key.last_used_at — the column that tells you a key is dead weight

Expiry (0020) says when a key stops working. This says whether anyone ever used
it, which is the question you actually ask before revoking: a key with no
last_used_at in ninety days is the one to kill first, and without the column the
only honest answer is "no idea".

Written at most once per KEY_TOUCH_INTERVAL (services/governance/credentials.py),
not per request. api_key is on the hottest path in the system — every REST call
and every MCP tool invocation resolves through it — and an unconditional UPDATE
there is a write amplifier that turns a read-mostly table into a contended one.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

UPGRADE = ["ALTER TABLE api_key ADD COLUMN last_used_at timestamptz"]
DOWNGRADE = ["ALTER TABLE api_key DROP COLUMN IF EXISTS last_used_at"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
