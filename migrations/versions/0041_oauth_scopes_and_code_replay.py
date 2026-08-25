"""oauth: token scopes, audience binding, and a single-use authorization-code store

Three columns and one table, closing the gap between what the OAuth surface
DOCUMENTED and what it enforced.

`api_key.scopes` — the `scope` parameter was accepted at /oauth/authorize and
thrown away: the consent screen named a grant that the minted token did not
carry. NULL/empty means unrestricted, which is what every existing `kur_`
service key and `dsto_` token is, so this is backwards compatible by
construction.

`api_key.resource` — the audience the token was minted for (RFC 8707). Honest
framing: opaque tokens looked up in our own store are already, structurally,
"issued for this server", so this closes a conformance gap rather than a live
hole. It earns its column in the narrow case that is real — two deployments
sharing one database (blue/green, a staging alias) where a token minted against
one base URL should not be honoured at the other.

`oauth_code_used` — the replay store. Authorization codes are stateless signed
JWTs whose docstrings called them "one-time" three times; nothing enforced it, so
a code was redeemable repeatedly inside its 120s window. OAuth 2.1 requires codes
be single-use AND that reuse revoke the token already issued from that code, which
needs the `minted_key_hash` column to find it. No RLS: like every other auth table
this is looked up before org context exists, on the admin engine.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE api_key ADD COLUMN scopes text[]",
    "ALTER TABLE api_key ADD COLUMN resource text",
    """CREATE TABLE oauth_code_used (
         jti text PRIMARY KEY,
         expires_at timestamptz NOT NULL,
         minted_key_hash text,
         used_at timestamptz NOT NULL DEFAULT now()
       )""",
    # Rows are only useful until the code they represent would have expired
    # anyway; the index is what makes the sweep cheap instead of a seq scan on a
    # table that grows with every sign-in.
    "CREATE INDEX ix_oauth_code_used_expiry ON oauth_code_used (expires_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_code_used TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS oauth_code_used",
    "ALTER TABLE api_key DROP COLUMN IF EXISTS resource",
    "ALTER TABLE api_key DROP COLUMN IF EXISTS scopes",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
