"""org.oidc_ref — the org key for generic-OIDC provisioning

Parallel to `clerk_ref` (migration 0006): a stable external reference so an OIDC
issuer's users provision and re-find one dst org idempotently. Separate column,
not a reuse of clerk_ref, because a deployment can have both configured and the two
refs must not collide.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE org ADD COLUMN oidc_ref text",
    "CREATE UNIQUE INDEX ix_org_oidc_ref ON org (oidc_ref) WHERE oidc_ref IS NOT NULL",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_org_oidc_ref",
    "ALTER TABLE org DROP COLUMN IF EXISTS oidc_ref",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
