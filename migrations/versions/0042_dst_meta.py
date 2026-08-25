"""dst_meta: install-wide key/value, for the encryption sentinel

The sentinel (services/security/sentinel.py) is a random UUID stored encrypted and
checked at boot, so a wrong DST_SECRET_KEY refuses to start instead of failing
one connector at a time, days later, for whoever is unlucky.

It cannot live in `setting`: that table is org-scoped under RLS, and the sentinel
is a property of the *install* — one key encrypts every tenant's credentials.
`embedding_meta` already established this shape (a global singleton outside RLS)
for the same reason; this is the general version of it.

No RLS, and only the admin engine touches it: like the auth tables it is read
before any org context exists.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

UPGRADE = [
    """CREATE TABLE dst_meta (
         key text PRIMARY KEY,
         value text NOT NULL,
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON dst_meta TO dst_app",
]

DOWNGRADE = ["DROP TABLE IF EXISTS dst_meta"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
