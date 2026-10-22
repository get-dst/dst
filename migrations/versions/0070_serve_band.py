"""serve_band: certified serve bands fitted per embedding model.

One row per embedding model (install-level, like embedding_meta: the
embedder is install config, not per org). `dst calibrate-bands` writes the
fit from the project's certified questions and a labelled paraphrase file;
every serve on an install whose embedder is that model reads it ahead of the
built-in presets and defaults.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE IF NOT EXISTS serve_band (
         model text PRIMARY KEY,
         exact double precision NOT NULL,
         equiv double precision NOT NULL,
         assist double precision NOT NULL,
         serves int NOT NULL,
         not_serves int NOT NULL,
         fitted_at timestamptz NOT NULL DEFAULT now(),
         CHECK (assist <= equiv AND equiv <= exact AND exact <= 1)
       )""",
    "GRANT SELECT ON serve_band TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS serve_band"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
