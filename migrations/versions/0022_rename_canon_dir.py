"""Rename the persisted config key canon_dir → certified_dir (lens-as-repo rename)

The "canon" concept became "certified definitions"; the Pydantic field
``ModelConfig.canon_dir`` is now ``certified_dir``. Existing lens bundles store the
old key in JSONB (``config.model.canon_dir``), which the renamed field would
silently ignore — dropping the binding. Rewrite the key in place across every
stored bundle: live + draft lens rows and version-history snapshots. A quoted-key
text replace is safe — ``"canon_dir"`` only ever appears as this JSON key.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _rename(old: str, new: str) -> list[str]:
    cols = [("lens", "draft_json"), ("lens", "published_json"), ("lens_version", "bundle_json")]
    return [
        f"""UPDATE {tbl} SET {col} = replace({col}::text, '"{old}"', '"{new}"')::jsonb
            WHERE {col}::text LIKE '%"{old}"%'"""
        for tbl, col in cols
    ]


def upgrade() -> None:
    for stmt in _rename("canon_dir", "certified_dir"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _rename("certified_dir", "canon_dir"):
        op.execute(stmt)
