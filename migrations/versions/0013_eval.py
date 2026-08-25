"""eval_case / eval_run: control-plane tables for the evaluation harness

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UPGRADE = [
    # ------------------------------------------------------------------ eval_case
    """CREATE TABLE eval_case (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         question text NOT NULL,
         expected_sql text NULL,
         expected_answer text NULL,
         snapshot_ref text NULL,
         source text NOT NULL,
         status text NOT NULL DEFAULT 'candidate',
         created_by text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE eval_case ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE eval_case FORCE ROW LEVEL SECURITY",
    """CREATE POLICY eval_case_isolation ON eval_case
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_eval_case_org_lens ON eval_case (org_id, lens)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON eval_case TO dst_app",
    # ------------------------------------------------------------------ eval_run
    """CREATE TABLE eval_run (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         lens text NOT NULL,
         lens_version text NULL,
         mode text NOT NULL,
         score double precision NULL,
         passed int NOT NULL DEFAULT 0,
         failed int NOT NULL DEFAULT 0,
         errored int NOT NULL DEFAULT 0,
         telemetry_ref text NULL,
         started_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE eval_run ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE eval_run FORCE ROW LEVEL SECURITY",
    """CREATE POLICY eval_run_isolation ON eval_run
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_eval_run_org_lens ON eval_run (org_id, lens)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON eval_run TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS eval_run",
    "DROP TABLE IF EXISTS eval_case",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
