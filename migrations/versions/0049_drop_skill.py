"""drop skill: server-side skill packs are retired

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-18

Every payload a pack carried has a first-class authored home — steering prose in
lens.yaml `instructions`, terms in semantic/definitions/, question→SQL pairs in
the certified store — so the pack machinery (table, API, serve-time merge) goes.
Pending `kind='skill'` patch candidates stay approvable: they become
`kind='instruction'` drafts targeting the lens itself, which is where approval
proposes the sentence now (the pack a candidate used to target no longer exists).

Downgrade recreates an EMPTY skill table (pack contents are not preserved) and
leaves retargeted patch candidates as instructions.
"""

from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

UPGRADE = [
    "UPDATE patch_candidate SET kind = 'instruction', target = lens WHERE kind = 'skill'",
    "DROP TABLE IF EXISTS skill",
]

DOWNGRADE = [
    """CREATE TABLE skill (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         name text NOT NULL,
         display_name text NOT NULL,
         description text NOT NULL DEFAULT '',
         instructions text NOT NULL DEFAULT '',
         definitions jsonb NOT NULL DEFAULT '[]'::jsonb,
         sample_queries jsonb NOT NULL DEFAULT '[]'::jsonb,
         version integer NOT NULL DEFAULT 1,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "CREATE UNIQUE INDEX ux_skill_org_name ON skill (org_id, name)",
    "ALTER TABLE skill ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE skill FORCE ROW LEVEL SECURITY",
    """CREATE POLICY skill_isolation ON skill
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON skill TO dst_app",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
