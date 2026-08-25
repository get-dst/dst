"""review: send-for-review tickets over the full reasoning trace

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE review (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         ticket_id text NOT NULL,
         request_id text NOT NULL,
         lens text NOT NULL,
         caller text NOT NULL,
         state text NOT NULL DEFAULT 'open',
         ai_verdict text,
         ai_reasoning text,
         human_verdict text,
         human_reasoning text,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "CREATE UNIQUE INDEX ux_review_org_ticket ON review (org_id, ticket_id)",
    "CREATE INDEX ix_review_org_state ON review (org_id, state)",
    "ALTER TABLE review ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE review FORCE ROW LEVEL SECURITY",
    """CREATE POLICY review_isolation ON review
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON review TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS review"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
