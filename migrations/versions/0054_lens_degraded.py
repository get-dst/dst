"""lens degraded: the serve-time drift mark that rides every answer until repair

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-18

When a governed query dies on a warehouse execution error and the
schema diff confirms drift, the lens is marked degraded — and the mark must
OUTLIVE the failing request, because the next answer that happens to touch an
unbroken table would otherwise serve clean over a broken layer (detection
otherwise waits on a user complaint, not the server). One nullable text
column: the human-readable degradation note (names the connection and the
ticket), appended to every subsequent response's `degraded` list. A successful
`dst apply` clears it — if the drift persists, the next serve error re-marks.
NULL = healthy, the steady state.
"""

from __future__ import annotations

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

UPGRADE = ["ALTER TABLE lens ADD COLUMN degraded text"]

DOWNGRADE = ["ALTER TABLE lens DROP COLUMN degraded"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
