"""certified_answer.verified_dialect — the dialect the probe verified against.

Certified SQL is dialect-bound text, but the row carried no provenance of the
warehouse that verified it: the probe executed and recorded only a value, the
serve path re-renders the stored SQL through the lens's *current* compiled
dialect, and every downstream guard is bypassed for certified answers. A lens
re-pointed at a different-dialect connection (the deploy-contract move) would
therefore serve duckdb-verified SQL through a snowflake round-trip silently.

``verified_dialect`` is stamped by the probe on successful execution and
checked at apply: compiled dialect ≠ pinned dialect is an apply error until a
re-probe on the new connection re-verifies the answer. Nullable: hand-certified
rows that were never executed carry no pin (their verification is advisory,
exactly as before).

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS verified_dialect text",
]
DOWNGRADE = [
    "ALTER TABLE certified_answer DROP COLUMN IF EXISTS verified_dialect",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
