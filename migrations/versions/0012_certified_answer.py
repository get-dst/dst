"""certified_answer: per-lens approved question→SQL repository (pgvector)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE certified_answer (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         question text NOT NULL,
         sql text NOT NULL,
         embedding vector(1024) NOT NULL,
         created_by text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE certified_answer ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE certified_answer FORCE ROW LEVEL SECURITY",
    """CREATE POLICY certified_answer_isolation ON certified_answer
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_certified_answer_org_lens ON certified_answer (org_id, lens)",
    "CREATE INDEX ix_certified_answer_embedding ON certified_answer "
    "USING hnsw (embedding vector_cosine_ops)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON certified_answer TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS certified_answer"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
