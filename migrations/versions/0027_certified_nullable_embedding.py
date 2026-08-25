"""certified_answer.embedding nullable — certify-from-review without an embedder.

A human APPROVE ruling must be promotable even when no embedding provider is
configured: the answer is stored with a NULL embedding (not served/matched until
`dst reindex` backfills it). NULL stops being reindex-only transient state
for this table, so its NOT NULL constraint goes away for good.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

UPGRADE = ["ALTER TABLE certified_answer ALTER COLUMN embedding DROP NOT NULL"]

DOWNGRADE = [
    # NULL-embedding rows (unembedded promotions) can't survive the constraint.
    "DELETE FROM certified_answer WHERE embedding IS NULL",
    "ALTER TABLE certified_answer ALTER COLUMN embedding SET NOT NULL",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
