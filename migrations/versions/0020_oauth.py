"""oauth: expiring tokens + dynamically-registered clients

MCP clients (Claude Code/Desktop) authenticate over OAuth: they dynamically
register, the person signs in via Clerk, and dst mints a short-lived `dsto_`
access token bound to a caller row. Those tokens reuse the existing `api_key`
store (one verification path for both service `kur_` keys and OAuth `dsto_`
tokens) — service keys keep `expires_at = NULL` (non-expiring), OAuth tokens set
it. `oauth_client` persists dynamic client registrations (RFC 7591). Both are
auth/infra looked up before org context exists -> no RLS, mirroring `api_key`.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE api_key ADD COLUMN expires_at timestamptz",
    """CREATE TABLE oauth_client (
         client_id text PRIMARY KEY,
         redirect_uris text[] NOT NULL,
         client_name text,
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_client TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS oauth_client",
    "ALTER TABLE api_key DROP COLUMN IF EXISTS expires_at",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
