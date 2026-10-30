"""Admit 'blueprint' as a shared semantic asset kind.

A table blueprint (semantic/tables/**.yaml) is a set of facts about one
warehouse table: its grain and key, rows that repeat per sync, its clocks,
joins and caveats. It shares the shared layer's whole lifecycle (upsert on
apply, export, content hash, staleness), so it lives in the same table.
Downgrade deletes the blueprint rows before narrowing the check — they have
no representation in the old shape.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-23
"""

from __future__ import annotations

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE semantic_asset DROP CONSTRAINT ck_semantic_asset_kind",
    """ALTER TABLE semantic_asset ADD CONSTRAINT ck_semantic_asset_kind
         CHECK (kind IN ('entity', 'definition', 'relationship', 'blueprint'))""",
]

DOWNGRADE = [
    "DELETE FROM semantic_asset WHERE kind = 'blueprint'",
    "ALTER TABLE semantic_asset DROP CONSTRAINT ck_semantic_asset_kind",
    """ALTER TABLE semantic_asset ADD CONSTRAINT ck_semantic_asset_kind
         CHECK (kind IN ('entity', 'definition', 'relationship'))""",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
