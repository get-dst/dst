"""table_profile / join_candidate: persisted table profiles + inferred join keys

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UPGRADE = [
    # -------------------------------------------------------------- table_profile
    # One row per (connection, table); `previous` keeps the prior payload so the
    # drift diff has something to compare against.
    """CREATE TABLE table_profile (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         connection text NOT NULL,
         table_name text NOT NULL,
         profile jsonb NOT NULL,
         previous jsonb NULL,
         profiled_at timestamptz NOT NULL DEFAULT now(),
         created_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (org_id, connection, table_name)
       )""",
    "ALTER TABLE table_profile ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE table_profile FORCE ROW LEVEL SECURITY",
    """CREATE POLICY table_profile_isolation ON table_profile
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_table_profile_org_connection ON table_profile (org_id, connection)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON table_profile TO dst_app",
    # ------------------------------------------------------------- join_candidate
    # Connection-scoped inferred join keys; the unique key dedupes re-discovered
    # candidates so a repeated catalog pass never resets an approval/rejection.
    """CREATE TABLE join_candidate (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         connection text NOT NULL,
         left_table text NOT NULL,
         left_columns jsonb NOT NULL,
         right_table text NOT NULL,
         right_columns jsonb NOT NULL,
         evidence text NOT NULL,
         overlap_ratio double precision NULL,
         status text NOT NULL DEFAULT 'candidate',
         created_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE (org_id, connection, left_table, left_columns, right_table, right_columns)
       )""",
    "ALTER TABLE join_candidate ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE join_candidate FORCE ROW LEVEL SECURITY",
    """CREATE POLICY join_candidate_isolation ON join_candidate
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_join_candidate_org_connection ON join_candidate (org_id, connection)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON join_candidate TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS join_candidate",
    "DROP TABLE IF EXISTS table_profile",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
