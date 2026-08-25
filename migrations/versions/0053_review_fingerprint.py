"""review fingerprint: one deduplicated ticket per standing incident

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-18

Drift and serve-error tickets describe a STANDING condition, not a
single request: the same broken column fails every question that reads it, and
filing a ticket per failure buries the one incident under its own repetitions
(a queue nobody could read). `fingerprint` is the deterministic identity of the incident —
sha256 over (connection, sorted schema deltas), or (connection, cause class)
when no drift explains the error — and the partial unique index makes "ONE
ticket per fingerprint" a database guarantee, not a check-before-insert race.
NULL for every ordinary ticket: those are per-request by design.
"""

from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE review ADD COLUMN fingerprint text",
    """CREATE UNIQUE INDEX ux_review_org_fingerprint ON review (org_id, fingerprint)
       WHERE fingerprint IS NOT NULL""",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ux_review_org_fingerprint",
    "ALTER TABLE review DROP COLUMN fingerprint",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
