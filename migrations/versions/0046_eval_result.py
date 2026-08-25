"""eval_result: per-case outcomes for an eval run (the drill-down behind the trend)

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

UPGRADE = [
    # `question` is denormalized from eval_case so the drill-down needs no join
    # (and survives the case being deleted); `case_id` is text, not a FK — a
    # certified-suite result carries a synthetic "certified:<answer_id>" id.
    """CREATE TABLE eval_result (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         run_id uuid NOT NULL REFERENCES eval_run(id) ON DELETE CASCADE,
         case_id text NOT NULL,
         question text NOT NULL DEFAULT '',
         passed boolean NOT NULL,
         grade text NULL,
         checks jsonb NULL,
         actual_sql text NULL,
         actual_value text NULL,
         reason text NULL,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE eval_result ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE eval_result FORCE ROW LEVEL SECURITY",
    """CREATE POLICY eval_result_isolation ON eval_result
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_eval_result_run ON eval_result (run_id)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON eval_result TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS eval_result",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
