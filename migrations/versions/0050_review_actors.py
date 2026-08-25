"""review actors: rulings record WHO, so the badge can cite evidence

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-18

A ruling without an actor is a verdict nobody stands behind. The review table
recorded verdict/reasoning/timestamps but not identity — any admin token could
rule and the ticket showed only "approved". Two additive columns fix the trail:

- `judged_by`  — the machine actor that produced ai_verdict (the judge's
  resolved model name, e.g. "deepseek-v4-flash"); '' when no judge ran.
- `ruled_by`   — who issued human_verdict, in actor convention:
  `human:<email>` for a dashboard session, `token:<label>` for a raw admin
  token. The `human:` prefix is load-bearing: a raw token is NOT provably a
  person, so it never claims it — trust tiers derive from this string.

Both default '' (not NULL): absence of an actor is a fact ("nobody ruled
yet"), not missing data.
"""

from __future__ import annotations

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE review ADD COLUMN judged_by text NOT NULL DEFAULT ''",
    "ALTER TABLE review ADD COLUMN ruled_by text NOT NULL DEFAULT ''",
]

DOWNGRADE = [
    "ALTER TABLE review DROP COLUMN ruled_by",
    "ALTER TABLE review DROP COLUMN judged_by",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
