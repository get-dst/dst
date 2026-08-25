"""review.correction + patch_candidate: correction deltas and drafted fixes

A correction records the wrong-vs-right delta on a review ticket; a
patch_candidate is the auto-drafted, human-approvable fix it produces.
ticket_id is nullable on purpose: the corpus distiller emits ticket-less
candidates through the same approval surface.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = [
    # ------------------------------------------------------------------ review
    # One jsonb column holds the structured CorrectionDelta (kind/note/corrected_*).
    "ALTER TABLE review ADD COLUMN correction jsonb",
    # --------------------------------------------------------- patch_candidate
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
         updated_at timestamptz NOT NULL DEFAULT now()
       )""",
    "ALTER TABLE patch_candidate ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE patch_candidate FORCE ROW LEVEL SECURITY",
    """CREATE POLICY patch_candidate_isolation ON patch_candidate
         USING (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)
         WITH CHECK (org_id = NULLIF(current_setting('app.current_org', true), '')::uuid)""",
    "CREATE INDEX ix_patch_candidate_org_lens_status ON patch_candidate (org_id, lens, status)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON patch_candidate TO dst_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS patch_candidate",
    "ALTER TABLE review DROP COLUMN IF EXISTS correction",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
