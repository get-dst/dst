"""certified_answer: active ⟹ embedded, structurally.

Four answers landed status='active' with embedding IS NULL — the write happened
while the install's embedder was absent. Certified matching is pgvector cosine,
so a NULL vector can never be returned: the corpus was active, verified,
invisible, and every surface reported success. The CHECK makes that state
unrepresentable; the UPDATE heals installs already carrying it by moving those
rows to the visible 'pending_embedding' status (`dst reindex` backfills the
vector and promotes them back to active).

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-06
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

UPGRADE = [
    "UPDATE certified_answer SET status = 'pending_embedding' "
    "WHERE embedding IS NULL AND status = 'active'",
    "ALTER TABLE certified_answer ADD CONSTRAINT certified_answer_active_embedded "
    "CHECK (status <> 'active' OR embedding IS NOT NULL)",
]

DOWNGRADE = [
    "ALTER TABLE certified_answer DROP CONSTRAINT IF EXISTS certified_answer_active_embedded",
    "UPDATE certified_answer SET status = 'active' WHERE status = 'pending_embedding'",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
