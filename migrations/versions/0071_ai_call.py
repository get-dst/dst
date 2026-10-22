"""ai_call: one row per provider call, with what it was for; eval spend columns.

A request's AI spend was one total on its request_log row, and an eval run's
was nowhere. Every provider call made while serving a question now lands
here: the model, the tokens the provider reported (prompt-cache reads and
writes apart), the price, and its purpose (route, decide, judge, equivalence,
bind, generate, compose). Test traffic is tagged by ``agent`` like
request_log's. eval_run and eval_result carry the tokens and cost of the
calls their cases made; a NULL cost is unpriced, never zero.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE IF NOT EXISTS ai_call (
         id bigserial PRIMARY KEY,
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         request_id text NOT NULL,
         lens text NULL,
         caller text NULL,
         agent text NULL,
         purpose text NOT NULL,
         provider text NOT NULL,
         model text NOT NULL,
         input_tokens int NOT NULL DEFAULT 0,
         output_tokens int NOT NULL DEFAULT 0,
         cache_read_tokens int NOT NULL DEFAULT 0,
         cache_write_tokens int NOT NULL DEFAULT 0,
         cost_usd double precision NULL,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE ai_call ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE ai_call FORCE ROW LEVEL SECURITY",
    """CREATE POLICY ai_call_isolation ON ai_call
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX IF NOT EXISTS ix_ai_call_org_created ON ai_call (org_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_ai_call_request ON ai_call (request_id)",
    "GRANT SELECT, INSERT, DELETE ON ai_call TO dst_app",
    "GRANT USAGE ON SEQUENCE ai_call_id_seq TO dst_app",
    "ALTER TABLE eval_run ADD COLUMN IF NOT EXISTS ai_input_tokens int",
    "ALTER TABLE eval_run ADD COLUMN IF NOT EXISTS ai_output_tokens int",
    "ALTER TABLE eval_run ADD COLUMN IF NOT EXISTS ai_cost_usd double precision",
    "ALTER TABLE eval_result ADD COLUMN IF NOT EXISTS ai_input_tokens int",
    "ALTER TABLE eval_result ADD COLUMN IF NOT EXISTS ai_output_tokens int",
    "ALTER TABLE eval_result ADD COLUMN IF NOT EXISTS ai_cost_usd double precision",
]

DOWNGRADE = [
    "ALTER TABLE eval_result DROP COLUMN IF EXISTS ai_cost_usd",
    "ALTER TABLE eval_result DROP COLUMN IF EXISTS ai_output_tokens",
    "ALTER TABLE eval_result DROP COLUMN IF EXISTS ai_input_tokens",
    "ALTER TABLE eval_run DROP COLUMN IF EXISTS ai_cost_usd",
    "ALTER TABLE eval_run DROP COLUMN IF EXISTS ai_output_tokens",
    "ALTER TABLE eval_run DROP COLUMN IF EXISTS ai_input_tokens",
    "DROP TABLE IF EXISTS ai_call",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
