"""embedding_meta: which embedder the stored vectors were made with.

One global row (embedders are install-level config, not per-org): the model name +
dimension every pgvector column's contents were produced by. The write path claims
it on first embed and refuses mismatched writes ("run `dst reindex`"); the
reindex flip updates it last. Installs that already hold vectors predate BYOK
embedder choice, so they are voyage-3.5/1024 by construction — seed that. Fresh
installs leave the table empty and claim on first write.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE embedding_meta (
         id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
         embedding_model text NOT NULL,
         embedding_dim int NOT NULL,
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "GRANT SELECT, INSERT, UPDATE ON embedding_meta TO dst_app",
    """INSERT INTO embedding_meta (embedding_model, embedding_dim)
       SELECT 'voyage-3.5', 1024
       WHERE EXISTS (SELECT 1 FROM context_chunk)
          OR EXISTS (SELECT 1 FROM certified_answer)""",
]

DOWNGRADE = ["DROP TABLE IF EXISTS embedding_meta"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
