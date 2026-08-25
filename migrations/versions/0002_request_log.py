"""request_log: the full per-response trace

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE request_log (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         request_id text NOT NULL,
         lens text NOT NULL,
         caller text NOT NULL,
         question text NOT NULL,
         sql text,
         valid boolean,
         row_count integer,
         sample jsonb,
         answer text,
         citations jsonb,
         definition_used text,
         confidence text,
         latency jsonb,
         ai_input_tokens integer,
         ai_output_tokens integer,
         ai_cost_usd double precision,
         wh_bytes bigint,
         wh_cost_usd double precision,
         status text NOT NULL,
         error text,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE request_log ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE request_log FORCE ROW LEVEL SECURITY",
    """CREATE POLICY request_log_isolation ON request_log
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_request_log_org_lens_created ON request_log (org_id, lens, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON request_log TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS request_log"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
