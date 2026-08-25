"""local_user + local_session: dashboard login without Clerk (OSS pivot, E3)

Self-hosters sign in with email+password; a login mints a `ksess_` session
token. `local_user` is a tenant table (RLS, mirroring caller in 0004);
`local_session` is looked up by token hash before org context exists, so —
exactly like `api_key` — it carries no RLS and verification uses the admin
engine.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE local_user (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         email text NOT NULL,
         password_hash text NOT NULL,
         role text NOT NULL DEFAULT 'member',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         CONSTRAINT uq_local_user_org_email UNIQUE (org_id, email),
         CONSTRAINT ck_local_user_role CHECK (role IN ('admin', 'member'))
       )""",
    "ALTER TABLE local_user ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE local_user FORCE ROW LEVEL SECURITY",
    """CREATE POLICY local_user_isolation ON local_user
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    # local_session is auth/infra (looked up by hash before org context exists) -> no RLS.
    """CREATE TABLE local_session (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         user_id uuid NOT NULL REFERENCES local_user(id) ON DELETE CASCADE,
         token_hash text NOT NULL UNIQUE,
         created_at timestamptz NOT NULL DEFAULT now(),
         expires_at timestamptz NOT NULL,
         revoked_at timestamptz
       )""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON local_user, local_session TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS local_session",
    "DROP TABLE IF EXISTS local_user",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
