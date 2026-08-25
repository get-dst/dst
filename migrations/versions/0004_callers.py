"""callers, api_keys, audit_log — the governance tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE caller (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         type text NOT NULL DEFAULT 'service',
         created_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_caller_org_name UNIQUE (org_id, name)
       )""",
    # api_key is auth/infra (looked up by hash before org context exists) -> no RLS.
    """CREATE TABLE api_key (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         caller_id uuid NOT NULL REFERENCES caller(id) ON DELETE CASCADE,
         key_hash text NOT NULL UNIQUE,
         prefix text NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now(),
         revoked_at timestamptz
       )""",
    """CREATE TABLE audit_log (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         caller text,
         lens text,
         decision text NOT NULL,
         reason text,
         ts timestamptz NOT NULL DEFAULT now()
       )""",
    # RLS on the tenant tables (caller, audit_log).
    "ALTER TABLE caller ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE caller FORCE ROW LEVEL SECURITY",
    """CREATE POLICY caller_isolation ON caller
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE audit_log FORCE ROW LEVEL SECURITY",
    """CREATE POLICY audit_log_isolation ON audit_log
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_audit_log_org_ts ON audit_log (org_id, ts)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON caller, api_key, audit_log TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS audit_log",
    "DROP TABLE IF EXISTS api_key",
    "DROP TABLE IF EXISTS caller",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
