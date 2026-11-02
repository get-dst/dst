"""caller.idp_subject: the identity-provider subject an OIDC sign-in bound.

A caller created or reused by the MCP consent step's IdP sign-in carries the
``sub`` claim of the verified ID token, so the next sign-in of the same person
reuses the row whatever their display name became, and ``dst keys sync-idp``
can revoke the callers the IdP no longer lists. Admin-created callers keep it
NULL.

Revision ID: 0077
Revises: 0076
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE caller ADD COLUMN IF NOT EXISTS idp_subject text")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_caller_org_idp_subject "
        "ON caller (org_id, idp_subject) WHERE idp_subject IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_caller_org_idp_subject")
    op.execute("ALTER TABLE caller DROP COLUMN IF EXISTS idp_subject")
