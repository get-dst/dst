"""Profile drift — what changed under a connection since the previous profile pass
of the stored catalog profiles.

The store keeps each table's previous profile next to the current one; the diff here
turns that into named drift items (column added/dropped/retyped, partition scheme
changed, a new enum literal, a table that stopped updating). Consequences are wired by
the callers: the drift endpoints surface items per connection and per lens, and a lens
with approved eval cases gets them named for re-baseline — never silently re-baselined
(that would launder a regression into the oracle).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.contracts.profile import TableProfile
from services.lenses import profile_store

DriftKind = Literal[
    "column_added",
    "column_dropped",
    "column_retyped",
    "partition_changed",
    "enum_value_added",
    "stale_table",
    # Whole tables appearing and disappearing. `diff_profiles` cannot see either
    # (it is handed one table's two versions) and the store's per-table rows have
    # nowhere to put them, so they are emitted only by the file-first baseline
    # differ — where the previous catalog is a whole snapshot, not a row.
    "table_added",
    "table_dropped",
]

# A table whose freshest known update is older than this is "stalled".
STALE_TABLE_DAYS = 7


class ProfileDrift(BaseModel):
    table: str
    kind: DriftKind
    detail: str


def diff_profiles(previous: TableProfile, current: TableProfile) -> list[ProfileDrift]:
    """Named differences between two profile versions of the same table."""
    out: list[ProfileDrift] = []
    prev_cols = {c.name: c for c in previous.columns}
    cur_cols = {c.name: c for c in current.columns}
    for name in sorted(cur_cols.keys() - prev_cols.keys()):
        out.append(ProfileDrift(table=current.table, kind="column_added", detail=name))
    for name in sorted(prev_cols.keys() - cur_cols.keys()):
        out.append(ProfileDrift(table=current.table, kind="column_dropped", detail=name))
    for name in sorted(prev_cols.keys() & cur_cols.keys()):
        prev_c, cur_c = prev_cols[name], cur_cols[name]
        if prev_c.type.lower() != cur_c.type.lower():
            out.append(
                ProfileDrift(
                    table=current.table,
                    kind="column_retyped",
                    detail=f"{name}: {prev_c.type} -> {cur_c.type}",
                )
            )
        if prev_c.top_values is not None and cur_c.top_values is not None:
            new_literals = sorted(set(cur_c.top_values) - set(prev_c.top_values))
            if new_literals:
                out.append(
                    ProfileDrift(
                        table=current.table,
                        kind="enum_value_added",
                        detail=f"{name}: {', '.join(new_literals)}",
                    )
                )
    prev_part = previous.partitioning.column if previous.partitioning else None
    cur_part = current.partitioning.column if current.partitioning else None
    if prev_part != cur_part:
        out.append(
            ProfileDrift(
                table=current.table,
                kind="partition_changed",
                detail=f"{prev_part or '(none)'} -> {cur_part or '(none)'}",
            )
        )
    return out


def _staleness(profile: TableProfile, *, stale_after_days: int) -> ProfileDrift | None:
    freshest = profile.last_updated_logical or profile.last_updated_physical
    if freshest is None:
        return None
    if freshest.tzinfo is None:
        freshest = freshest.replace(tzinfo=UTC)
    age = datetime.now(UTC) - freshest
    if age <= timedelta(days=stale_after_days):
        return None
    return ProfileDrift(
        table=profile.table,
        kind="stale_table",
        detail=f"last update {freshest.date().isoformat()} ({age.days} days ago)",
    )


def connection_drift(
    session: Session, connection: str, *, stale_after_days: int = STALE_TABLE_DAYS
) -> list[ProfileDrift]:
    """All drift items for a connection: previous-vs-current diffs + stalled tables."""
    out: list[ProfileDrift] = []
    for stored in profile_store.list_profiles(session, connection):
        if stored.previous is not None:
            out.extend(diff_profiles(stored.previous, stored.profile))
        stalled = _staleness(stored.profile, stale_after_days=stale_after_days)
        if stalled is not None:
            out.append(stalled)
    return out
