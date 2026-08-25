"""lens_version: immutable published-version history (lens-as-repo)

Each publish snapshots the validated bundle here, so a lens carries full history
(browse + diff in the Repo tab) instead of a single overwritten ``published_json``.
``bundle_json`` is the canonical artifact; the file tree is re-derived from it on
read, so the materializer can evolve without a data migration. Org-scoped via RLS,
mirroring audit_run (0018).

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE lens_version (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         version integer NOT NULL,
         bundle_json jsonb NOT NULL,
         summary text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_lens_version UNIQUE (org_id, lens, version)
       )""",
    "ALTER TABLE lens_version ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE lens_version FORCE ROW LEVEL SECURITY",
    """CREATE POLICY lens_version_isolation ON lens_version
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    # list_versions / next-version are filtered by (org, lens), newest first.
    "CREATE INDEX ix_lens_version_lens ON lens_version (org_id, lens, version DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON lens_version TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS lens_version",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
