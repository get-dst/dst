"""eval_case.expect / .term — behavioral expectations get real columns.

Behavioral cases (``expect: clarify | refuse`` + optional ``term``) shipped
riding the ``expected_answer`` column as a marker string ("expect:clarify
term=value"), deferring the schema change. This is that migration: the two
fields get their own nullable columns and ``expected_answer`` goes back to being
what its name says — a prose oracle no scorer reads.

Existing marker rows convert in place and have their ``expected_answer`` cleared.
The rewrite mirrors ``parse_behavioral_marker`` exactly (prefix, the two valid
kinds, optional ``" term="`` tail, blank term is no term) and is guarded on both
``expect IS NULL`` and the marker shape, so re-running it touches nothing.
Alembic runs as the admin superuser, which bypasses eval_case's FORCE'd RLS —
the rewrite sees every org's rows, no GUC to set.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

UPGRADE = [
    "ALTER TABLE eval_case ADD COLUMN IF NOT EXISTS expect text",
    "ALTER TABLE eval_case ADD COLUMN IF NOT EXISTS term text",
    # The term pattern deliberately carries no '(?:...)' group: alembic runs
    # these through SQLAlchemy text(), whose bind-parameter regex would read
    # ':clarify' as a parameter and mangle the statement. The WHERE clause has
    # already pinned the marker shape, so matching the ' term=' tail alone is
    # exact — and, like the Python decoder's partition(), it splits on the first.
    """UPDATE eval_case
          SET expect = substring(expected_answer from '^expect:(clarify|refuse)'),
              term = NULLIF(
                  btrim(coalesce(substring(expected_answer from ' term=(.*)$'), '')),
                  ''
              ),
              expected_answer = NULL
        WHERE expect IS NULL
          AND expected_answer ~ '^expect:(clarify|refuse)( term=.*)?$'""",
]

DOWNGRADE = [
    # Re-encode before dropping: an older wheel reads behavioral cases only as
    # markers, so a downgrade that just dropped the columns would silently
    # demote every pin to a value case.
    """UPDATE eval_case
          SET expected_answer = 'expect:' || expect || coalesce(' term=' || term, ''),
              expect = NULL,
              term = NULL
        WHERE expect IS NOT NULL""",
    "ALTER TABLE eval_case DROP COLUMN IF EXISTS term",
    "ALTER TABLE eval_case DROP COLUMN IF EXISTS expect",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
