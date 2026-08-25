"""router_anchor: publish-time coverage-profile anchor embeddings (pgvector)

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-19

The router used to rebuild and RE-EMBED every lens's
coverage-profile anchors on every /v1/query request. These rows are the
publish-time home for those vectors — a write-through cache keyed
(org, lens, anchor): publish pre-warms it, the read path repairs drift, and
the request path embeds only the question. Scoring runs where the vectors
live (max cosine per lens), so no hnsw index: an org's anchors are a few
hundred rows, scanned whole. `dst reindex` empties the table (cache — it
rebuilds lazily under the new embedder).
"""

from __future__ import annotations

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE router_anchor (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         anchor text NOT NULL,
         embedding vector(1024) NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (org_id, lens, anchor)
       )""",
    "ALTER TABLE router_anchor ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE router_anchor FORCE ROW LEVEL SECURITY",
    """CREATE POLICY router_anchor_isolation ON router_anchor
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_router_anchor_org_lens ON router_anchor (org_id, lens)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON router_anchor TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS router_anchor"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
