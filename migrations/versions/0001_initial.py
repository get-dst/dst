"""initial: org, admin_token, setting; app role; RLS on setting

Revision ID: 0001
Revises:
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    # Non-superuser application role (so RLS is enforced). Idempotent. Created
    # NOLOGIN with no password: a baked-in default would be a public credential
    # on every install. `dst migrate` grants LOGIN with the password DATABASE_URL
    # declares (services/db/app_role.py) right after upgrading.
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dst_app') THEN
           CREATE ROLE dst_app NOLOGIN NOSUPERUSER;
         END IF;
       END $$""",
    "GRANT USAGE ON SCHEMA public TO dst_app",
    # Future tables created by the migration owner auto-grant to the app role.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dst_app",
    """CREATE TABLE org (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         name text NOT NULL,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    """CREATE TABLE admin_token (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         token_hash text NOT NULL UNIQUE,
         label text,
         created_at timestamptz NOT NULL DEFAULT now(),
         revoked_at timestamptz
       )""",
    """CREATE TABLE setting (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         key text NOT NULL,
         value jsonb,
         created_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_setting_org_key UNIQUE (org_id, key)
       )""",
    # RLS on the tenant table (setting). org/admin_token are auth/infra (no RLS).
    "ALTER TABLE setting ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE setting FORCE ROW LEVEL SECURITY",
    """CREATE POLICY setting_isolation ON setting
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON org, setting TO dst_app",
    # admin_token is read-only for the app role: auth resolution needs SELECT,
    # but minting/revoking control-plane tokens is admin-engine work
    # (bootstrap, `dst revoke-key`). An app role that could INSERT here could
    # grant itself org admin. The REVOKE undoes the default-privileges
    # auto-grant above, which already applied at CREATE TABLE.
    "GRANT SELECT ON admin_token TO dst_app",
    "REVOKE INSERT, UPDATE, DELETE ON admin_token FROM dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS setting",
    "DROP TABLE IF EXISTS admin_token",
    "DROP TABLE IF EXISTS org",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
