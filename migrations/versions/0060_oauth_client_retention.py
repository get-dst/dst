"""oauth_client retention: record use, so an unused registration can be reaped

`POST /oauth/register` is unauthenticated by construction — RFC 7591 dynamic
client registration is how an MCP client bootstraps before anyone has signed in
— so anyone who can reach the port can append rows to `oauth_client`, and
nothing removed them. The table only grew.

Reaping needs to tell a registration that went somewhere from one that never
did, which the old shape could not: `last_used_at` records the moment a
client_id is first carried into an authorization request. A row that has one is
a real client and is never touched by retention; a row that never got one is
abandoned (or was never a client at all) and expires. The index serves the
sweep, which runs opportunistically on registration.

Revision ID: 0060
Revises: 0059
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE oauth_client ADD COLUMN last_used_at timestamptz",
    # Partial: the sweep only ever looks at rows that were never used, and on a
    # healthy install those are the minority.
    "CREATE INDEX oauth_client_unused_idx ON oauth_client (created_at) WHERE last_used_at IS NULL",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS oauth_client_unused_idx",
    "ALTER TABLE oauth_client DROP COLUMN IF EXISTS last_used_at",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
