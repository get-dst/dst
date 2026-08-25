"""routing_decision: every lens-less /v1/query routing decision

The router is a managed system lens whose answer is a route. Every decision —
a route to a covering lens or an honest decline — is persisted here so surface
area (routed / asked) and the uncovered-question clusters can be computed per
org over a window. routed_lens is NULL on a decline; score is the
best-covering similarity (the nearest-miss score on a decline). Org-scoped via
RLS, mirroring audit_run (0018).

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE routing_decision (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         question text NOT NULL,
         routed_lens text,
         score double precision NOT NULL,
         covered boolean NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE routing_decision ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE routing_decision FORCE ROW LEVEL SECURITY",
    """CREATE POLICY routing_decision_isolation ON routing_decision
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    # The surface rollup filters by (org, window) newest-first — the hot path.
    "CREATE INDEX ix_routing_decision_org_created ON routing_decision (org_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON routing_decision TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS routing_decision",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
