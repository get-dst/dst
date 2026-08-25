"""Drop the prose-context chunk store.

Prose-context ingestion left the product (2026-08-25): curated context is the
semantic model, the governed definitions, and the certified-definition pages,
all file-authored. The chunk table and its vector index go with it. Downgrade
recreates the exact 0005 shape (RLS, policy, indexes, grants) so a rollback
keeps migrating cleanly — the chunks themselves were derived data.

Revision ID: 0061
Revises: 0060
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

UPGRADE = ["DROP TABLE IF EXISTS context_chunk"]

DOWNGRADE = [
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


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
