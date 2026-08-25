"""org.name unique: the by-name selectors stop being ambiguous

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-17

`--org NAME` scopes CLI verbs — including `revoke-key`, an access-control
write — through `SELECT id FROM org WHERE name = :n ORDER BY created_at
LIMIT 1`. With duplicate names that silently targets the OLDEST org — a
silent-wrong shape, so the ambiguity dies here, at the schema, not in every
caller. Duplicates accumulate easily: a crashed run leaves an orphan org behind
and every by-name lookup afterwards resolves to a stale empty tenant. With this
index the duplicate fails the very next INSERT, loudly, instead of poisoning
whoever runs later.

An install that already holds duplicates fails this migration with postgres
naming the duplicated value — rename one org and re-run `dst migrate`.
"""

from __future__ import annotations

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE org ADD CONSTRAINT uq_org_name UNIQUE (name)",
]

DOWNGRADE = [
    "ALTER TABLE org DROP CONSTRAINT IF EXISTS uq_org_name",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
