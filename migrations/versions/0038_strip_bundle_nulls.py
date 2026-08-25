"""Drop null-valued keys from stored lens bundles — rollback readability.

`ModelConfig.provider`/`model`/`temperature` widened from `str`/`float` to optional,
and apply wrote literal `null` into `lens.draft_json`, `lens.published_json` and
`lens_version.bundle_json` for every lens with no `model:` block. The previous
release types those fields as plain `str`/`float`, so it raised ValidationError on
every read — and `plan` enumerates every published bundle, so the entire project
surface 500'd and the old code could not repair its own project. Recovery was a
`pg_restore`. Storage now omits unset keys (services/lenses/store.py::_stored); this
repairs the rows already written, `lens_version` included — it is immutable history,
so a bundle left null-bearing there is permanently unreadable by the release that
wrote it.

`jsonb_strip_nulls` is exactly the storage rule, in SQL: a key whose value is null
carries no information a reader cannot get from its own default, at any depth.

No downgrade: re-inserting keys that carried nothing would only rebuild the hazard.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_COLUMNS = (("lens", "draft_json"), ("lens", "published_json"), ("lens_version", "bundle_json"))


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.execute(
            f"UPDATE {table} SET {column} = jsonb_strip_nulls({column}) "
            f"WHERE {column} IS NOT NULL AND {column} <> jsonb_strip_nulls({column})"
        )


def downgrade() -> None:
    """Deliberately empty — the stripped keys held no value to restore."""
