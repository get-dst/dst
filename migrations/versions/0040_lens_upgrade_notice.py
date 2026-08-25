"""lens.upgrade_notice — the slot for "this release changed what your file means".

`dst plan` answers "what is different between my files and the server", so a
release that changes how an UNCHANGED file is INTERPRETED is invisible to it: it
reports `unchanged` and the behaviour moves anyway. This column is where such a
release leaves one line, on exactly the lenses it moved. ``publish`` clears it
(services/lenses/store.py), so the notice lives until its owner next applies and
never becomes standing noise.

Its first occupant, and the reason it exists: ``ModelConfig.temperature`` used to
have no readers — ``generation_temperature()`` returned the answer_mode's value
unconditionally, so a lens declaring ``temperature: 0.0`` generated at 0.2. That
was fixed once a determinism run caught a lens sampling at 0.2 while its config
asked for 0.0. Correct fix, silent transition:
every bundle published before it changes sampling the moment its server is
upgraded, in either direction — `dst init` wrote ``temperature: 0.0`` under a
``balanced`` default (0.2 -> 0.0), and a ``strict`` lens carrying the scaffold's
old ``temperature: 0.2`` moves 0.0 -> 0.2, i.e. LESS deterministic on the mode
that exists to be deterministic.

Stamped here rather than computed at plan time on a date, because the date that
matters is when THIS server upgraded, and only a migration knows it: a lens
published yesterday under old code has a wall-clock timestamp that no calendar
cutoff can distinguish from one published under new.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-07
"""

from __future__ import annotations

from typing import Any

from alembic import op
from sqlalchemy import text

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

# generation_temperature()'s answer_mode table, frozen as of this migration.
_MODE_TEMPERATURE = {"strict": 0.0, "balanced": 0.2, "exploratory": 0.5}


def temperature_notice(model: dict[str, Any]) -> str | None:
    """The line for one stored ``config.model``, or None when nothing moved.

    Only a lens that PINS a temperature its answer_mode does not already supply
    changed behaviour: an unset temperature always followed the mode, and a pin
    equal to the mode's value produces the same number either way."""
    pinned = model.get("temperature")
    if isinstance(pinned, bool) or not isinstance(pinned, int | float):
        return None  # unset (the default): the lens always followed answer_mode
    mode = str(model.get("answer_mode") or "balanced")
    was = _MODE_TEMPERATURE.get(mode)
    if was is None or float(pinned) == was:
        return None  # no behaviour change: the pin already matched the mode
    return (
        f"generation temperature: this lens generates at {float(pinned)} now and generated "
        f"at {was} before this upgrade — `temperature: {float(pinned)}` in its config had "
        f"no reader before this upgrade (answer_mode: {mode} supplied {was}) and is live now. "
        f"Nothing to fix if {float(pinned)} is what it meant; remove `model.temperature` "
        f"from lens.yaml to go back to {was}. Clears on the next apply."
    )


def upgrade() -> None:
    op.execute("ALTER TABLE lens ADD COLUMN IF NOT EXISTS upgrade_notice text")
    conn = op.get_bind()
    for row_id, bundle in conn.execute(
        text("SELECT id, published_json FROM lens WHERE published_json IS NOT NULL")
    ):
        notice = temperature_notice(((bundle or {}).get("config") or {}).get("model") or {})
        if notice is not None:
            conn.execute(
                text("UPDATE lens SET upgrade_notice = :n WHERE id = :i"),
                {"n": notice, "i": row_id},
            )


def downgrade() -> None:
    op.execute("ALTER TABLE lens DROP COLUMN IF EXISTS upgrade_notice")
