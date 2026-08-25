"""context_chunk: per-lens RAG store (pgvector)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE context_chunk (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         source text NOT NULL,
         text text NOT NULL,
         embedding vector(1024) NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE context_chunk ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE context_chunk FORCE ROW LEVEL SECURITY",
    """CREATE POLICY context_chunk_isolation ON context_chunk
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_context_chunk_org_lens ON context_chunk (org_id, lens)",
    "CREATE INDEX ix_context_chunk_embedding ON context_chunk "
    "USING hnsw (embedding vector_cosine_ops)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON context_chunk TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS context_chunk"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
