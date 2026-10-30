"""Drop patch_candidate: server-side patch drafting is retired.

A patch candidate was the auto-drafted, human-approvable fix a correction or
the corpus distiller produced. Drafting the fix is authoring judgment that the
reviewer's own agent makes from the ticket and the files; the review queue
(tickets, rulings, certify-on-approve) stays. Nothing reads the table any more.

Downgrade recreates an EMPTY table in its final shape (0017 + 0034); candidates
are not preserved.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

UPGRADE = ["DROP TABLE IF EXISTS patch_candidate"]

DOWNGRADE = [
    """CREATE TABLE patch_candidate (
         id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         org_id uuid NOT NULL REFERENCES org(id) ON DELETE CASCADE,
         ticket_id text NULL,
         lens text NOT NULL,
         kind text NOT NULL,
         target text NOT NULL,
         owner text NOT NULL DEFAULT 'lens-owner',
         diff_before text NULL,
         diff_after text NOT NULL,
         status text NOT NULL DEFAULT 'candidate',
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         rejection_note text
       )""",
    "ALTER TABLE patch_candidate ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE patch_candidate FORCE ROW LEVEL SECURITY",
    """CREATE POLICY patch_candidate_isolation ON patch_candidate
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_patch_candidate_org_lens_status ON patch_candidate (org_id, lens, status)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON patch_candidate TO dst_app",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
