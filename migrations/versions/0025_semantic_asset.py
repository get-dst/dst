"""semantic_asset: the project-level shared semantic layer

Entities and definitions live once at org scope; lenses select them and apply
compiles the selection into each lens's embedded model. One table for both
kinds — they share the whole lifecycle (upsert-on-apply, export, content-hash,
dependent-lens delete guard). org_standard is subsumed: its rows migrate in as
shared definitions and the table drops (services/definitions/standards.py now
reads/writes here behind the unchanged OrgStandard API). content_hash '' means
"recompute on next write" — migrated rows read as stale until first apply.
Org-scoped via RLS, mirroring lens_version (0021).

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE semantic_asset (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         kind text NOT NULL,
         name text NOT NULL,
         body jsonb NOT NULL,
         content_hash text NOT NULL DEFAULT '',
         source text NOT NULL DEFAULT 'authored',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_semantic_asset UNIQUE (org_id, kind, name),
         CONSTRAINT ck_semantic_asset_kind CHECK (kind IN ('entity', 'definition'))
       )""",
    "ALTER TABLE semantic_asset ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE semantic_asset FORCE ROW LEVEL SECURITY",
    """CREATE POLICY semantic_asset_isolation ON semantic_asset
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_semantic_asset_org_kind ON semantic_asset (org_id, kind, name)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON semantic_asset TO dst_app",
    # org_standard rows become shared definitions (Definition body shape).
    """INSERT INTO semantic_asset (org_id, kind, name, body)
       SELECT org_id, 'definition', term,
              jsonb_build_object(
                'term', term, 'body', body, 'sql_expr', sql_expr,
                'source', 'authored', 'status', 'active',
                'possible_mappings', '[]'::jsonb)
       FROM org_standard""",
    "DROP TABLE org_standard",
]

DOWNGRADE = [
    # Recreate org_standard (0008 shape) and move definitions back.
    """CREATE TABLE org_standard (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         term text NOT NULL,
         body text NOT NULL,
         sql_expr text,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_org_standard UNIQUE (org_id, term)
       )""",
    "ALTER TABLE org_standard ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE org_standard FORCE ROW LEVEL SECURITY",
    """CREATE POLICY org_standard_isolation ON org_standard
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON org_standard TO dst_app",
    """INSERT INTO org_standard (org_id, term, body, sql_expr)
       SELECT org_id, name, body->>'body', body->>'sql_expr'
       FROM semantic_asset WHERE kind = 'definition'""",
    "DROP TABLE IF EXISTS semantic_asset",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
