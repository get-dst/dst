"""audit_run: persisted Definition Drift audit runs

The wrongness probe stops being a button-press and becomes standing results:
each audit (manual refresh now, scheduled later) records a row here, and the
dashboard default-loads the latest run per connection. findings holds the
DriftFinding list verbatim (the report payload); status is 'ok' for a completed
run. Org-scoped via RLS, mirroring patch_candidate (0017).

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE audit_run (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         connection text NOT NULL,
         days integer NOT NULL,
         records_scanned integer NOT NULL,
         findings jsonb NOT NULL,
         status text NOT NULL DEFAULT 'ok',
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE audit_run ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE audit_run FORCE ROW LEVEL SECURITY",
    """CREATE POLICY audit_run_isolation ON audit_run
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    # latest(connection) is the hot path: filter by (org, connection), newest first.
    "CREATE INDEX ix_audit_run_org_conn_created ON audit_run (org_id, connection, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON audit_run TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS audit_run",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
