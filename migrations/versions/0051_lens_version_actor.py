"""lens_version.created_by: the publish history names who published

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-18

The lens's version history (0021) records what changed and when — never who.
Same actor convention as review rulings (0050): `human:<email>` for a
dashboard session, `token:<label>` for a raw admin token, `process:<id>` for
server-initiated publishes (recompiles). '' = recorded before this migration
or by a path with no identity — an honest unknown, not missing data.
"""

from __future__ import annotations

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE lens_version ADD COLUMN created_by text NOT NULL DEFAULT ''",
]

DOWNGRADE = [
    "ALTER TABLE lens_version DROP COLUMN created_by",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
