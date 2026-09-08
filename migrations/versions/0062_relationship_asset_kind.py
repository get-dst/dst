"""Admit 'relationship' as a shared semantic asset kind.

Relationships moved out of the entity files into their own assets
(semantic/relationships/<left>__<right>.yaml): a join is a fact about a pair,
and giving the pair one home kills the two-files-competing-claims class at the
schema instead of a validator. Downgrade deletes the relationship rows before
narrowing the check — they have no representation in the old shape.

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE semantic_asset DROP CONSTRAINT ck_semantic_asset_kind",
    """ALTER TABLE semantic_asset ADD CONSTRAINT ck_semantic_asset_kind
         CHECK (kind IN ('entity', 'definition', 'relationship'))""",
]

DOWNGRADE = [
    "DELETE FROM semantic_asset WHERE kind = 'relationship'",
    "ALTER TABLE semantic_asset DROP CONSTRAINT ck_semantic_asset_kind",
    """ALTER TABLE semantic_asset ADD CONSTRAINT ck_semantic_asset_kind
         CHECK (kind IN ('entity', 'definition'))""",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
