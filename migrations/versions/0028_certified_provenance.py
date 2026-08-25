"""certified_answer provenance — source, verified_by, derived bindings.

Certified answers imported from a BI corpus (or promoted from review) must carry
inspectable provenance: ``source`` (where the pair came from, e.g.
"looker:dashboards/42 'MRR'" or "review:<request_id>"), ``verified_by`` (who/what
vouches — the authority, distinct from created_by's acting identity), and
``bindings`` — the {asset_key: content_hash} map of the shared assets the SQL
touches, computed at apply/certify time (never authored, never rendered to files).
All nullable: existing rows and old yaml files load unchanged; bindings backfill
on the next apply.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS source text",
    "ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS verified_by text",
    "ALTER TABLE certified_answer ADD COLUMN IF NOT EXISTS bindings jsonb",
]

DOWNGRADE = [
    "ALTER TABLE certified_answer DROP COLUMN IF EXISTS bindings",
    "ALTER TABLE certified_answer DROP COLUMN IF EXISTS verified_by",
    "ALTER TABLE certified_answer DROP COLUMN IF EXISTS source",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
