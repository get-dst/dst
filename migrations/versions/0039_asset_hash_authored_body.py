"""Restamp the shared-asset digests that the authored-body hash re-bases.

``asset_content_hash`` now covers an asset's AUTHORED body instead of its
serialized model, so a schema addition stops moving every asset's digest
(services/contracts/shared_semantic.py). The digest is recorded in three
derived places, all written by the old rule:

* ``semantic_asset.content_hash`` — the asset's own record;
* ``certified_answer.bindings`` — the hashes an answer was VERIFIED against;
* every bundle's ``semantic_model.shared_provenance.assets`` — what a lens was
  compiled from.

Left alone they would all mismatch on the first plan after upgrade — exactly
the false "re-verify these certified answers" this change exists to remove. So
restamp them, by the only rule that is honest in both directions: a recorded
digest that EQUALS the asset's stored ``content_hash`` was in sync, and is
rewritten to the asset's new digest; one that differs had genuinely drifted
since it was recorded, and is left alone so it still flags.

Downgrade cannot restore the old digests (they are derived, and the old values
are gone) — it is a no-op. Reverting the code without a pre-upgrade restore
leaves every lens reading stale until the next apply recompiles it; the
downgrade path was already a one-way door for other reasons.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-07
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
from sqlalchemy import text

from services.contracts.shared_semantic import asset_content_hash

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def _restamp(assets: dict[str, Any], moved: dict[str, tuple[str, str]]) -> bool:
    """Rewrite in place every digest that still matches the asset's old one."""
    changed = False
    for key, recorded in list(assets.items()):
        pair = moved.get(key)
        if pair is not None and recorded == pair[0]:
            assets[key] = pair[1]
            changed = True
    return changed


def restamp_all(conn: Any) -> None:
    """The whole migration, against any connection — so the regression test can
    run the real thing instead of a paraphrase of it."""
    rows = conn.execute(
        text("SELECT id, org_id, kind, name, body, content_hash FROM semantic_asset")
    )
    # key -> (old digest, new digest), per org: asset names are unique per org,
    # and a digest is content-derived, so one map across orgs would still be
    # right — but scoping it per org keeps a same-named asset in another org
    # from restamping this one's bindings.
    moved: dict[str, dict[str, tuple[str, str]]] = {}
    for asset_id, org_id, kind, name, body, old in rows:
        new = asset_content_hash(kind, body)
        if new == old:
            continue
        moved.setdefault(str(org_id), {})[f"{kind}/{name}"] = (old, new)
        conn.execute(
            text("UPDATE semantic_asset SET content_hash = :h WHERE id = :i"),
            {"h": new, "i": asset_id},
        )
    if not moved:
        return

    for org_id, org_moved in moved.items():
        for answer_id, bindings in conn.execute(
            text(
                "SELECT id, bindings FROM certified_answer "
                "WHERE org_id = CAST(:o AS uuid) AND bindings IS NOT NULL"
            ),
            {"o": org_id},
        ):
            if isinstance(bindings, dict) and _restamp(bindings, org_moved):
                conn.execute(
                    text("UPDATE certified_answer SET bindings = CAST(:b AS jsonb) WHERE id = :i"),
                    {"b": json.dumps(bindings), "i": answer_id},
                )

        # Bundles: the live pair on `lens` plus the version history, the same
        # three columns migration 0022 had to rewrite for the canon_dir rename.
        for table, col in (
            ("lens", "draft_json"),
            ("lens", "published_json"),
            ("lens_version", "bundle_json"),
        ):
            for row_id, bundle in conn.execute(
                text(
                    f"SELECT id, {col} FROM {table} "
                    f"WHERE org_id = CAST(:o AS uuid) AND {col} IS NOT NULL"
                ),
                {"o": org_id},
            ):
                provenance = ((bundle or {}).get("semantic_model") or {}).get("shared_provenance")
                assets = (provenance or {}).get("assets")
                if isinstance(assets, dict) and _restamp(assets, org_moved):
                    conn.execute(
                        text(f"UPDATE {table} SET {col} = CAST(:b AS jsonb) WHERE id = :i"),
                        {"b": json.dumps(bundle), "i": row_id},
                    )


def upgrade() -> None:
    restamp_all(op.get_bind())


def downgrade() -> None:
    """No-op: the old digests are derived and gone. The next apply under old
    code recomputes them (recompiling every lens once)."""
