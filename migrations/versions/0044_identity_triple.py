"""the identity triple: who, through what, and the join between decision and query

`caller` on both logs was a single string — and for an agent-driven request that
string is the *credential's* name (the person), with no record of the agent acting
for them. Attribution needs two separable facts, and a way to join them:

- `agent` (both logs) — the acting client. Self-asserted and a LABEL, never a
  security input (the MCP spec is explicit that clientInfo MUST NOT drive security
  decisions). Its worth is the audit answer "person X, *through* agent Y".
- `request_id` on `audit_log` — `request_log` already has one; `audit_log` did not,
  so the allow/deny decision and the query it authorized were unjoinable. With it,
  "X, via Y, asked Q, was allowed on lens L, ran SQL S" is one join.

A denied request records its `request_id` on the audit row even though no
`request_log` row exists for it — honest and null-joinable, not a gap.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE audit_log ADD COLUMN request_id text",
    "ALTER TABLE audit_log ADD COLUMN agent text",
    "ALTER TABLE request_log ADD COLUMN agent text",
]

DOWNGRADE = [
    "ALTER TABLE request_log DROP COLUMN IF EXISTS agent",
    "ALTER TABLE audit_log DROP COLUMN IF EXISTS agent",
    "ALTER TABLE audit_log DROP COLUMN IF EXISTS request_id",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
