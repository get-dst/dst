"""Stamp the lenses the balanced-mode determinism change moves.

``answer_mode: balanced`` (the default) now generates SQL at temperature 0.0
instead of 0.2: generation is a computation — sampled generation returned
slightly different totals for the same question across runs, produced two
response SHAPES for one ask, and `dst test` diffs regenerated SQL, so it made
the eval gate itself flaky. `exploratory` (or an explicit `temperature:`) remains the stated
opt-out.

The 0040 doctrine applies verbatim: a release that changes how an UNCHANGED
file is interpreted must leave one line on exactly the lenses it moved, because
`dst plan` truthfully reports `unchanged` while behaviour shifts. Affected:
lenses with no pinned ``temperature`` whose mode is ``balanced`` (explicitly or
by default) — a pin always won, and strict/exploratory modes are untouched.

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Any

from alembic import op
from sqlalchemy import text

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def balanced_default_notice(model: dict[str, Any]) -> str | None:
    """The line for one stored ``config.model``, or None when nothing moved."""
    pinned = model.get("temperature")
    if isinstance(pinned, int | float) and not isinstance(pinned, bool):
        return None  # an explicit pin always won; it still does
    if str(model.get("answer_mode") or "balanced") != "balanced":
        return None  # strict already generated at 0.0; exploratory keeps 0.5
    return (
        "generation temperature: this lens generates at 0.0 now and generated at 0.2 "
        "before this upgrade — answer_mode: balanced defaults to deterministic SQL "
        "generation now (sampled generation drifted counts across identical asks and "
        "made the eval gate flaky). Nothing to fix if deterministic is what you want; "
        "set `model.temperature: 0.2` or `answer_mode: exploratory` in lens.yaml to "
        "sample again. Clears on the next apply."
    )


def upgrade() -> None:
    conn = op.get_bind()
    for row_id, bundle in conn.execute(
        text("SELECT id, published_json FROM lens WHERE published_json IS NOT NULL")
    ):
        notice = balanced_default_notice(((bundle or {}).get("config") or {}).get("model") or {})
        if notice is not None:
            conn.execute(
                text("UPDATE lens SET upgrade_notice = :n WHERE id = :i"),
                {"n": notice, "i": row_id},
            )


def downgrade() -> None:
    # The stamp is advisory text; nothing structural to undo.
    op.execute(
        "UPDATE lens SET upgrade_notice = NULL WHERE upgrade_notice LIKE "
        "'generation temperature: this lens generates at 0.0 now%'"
    )
