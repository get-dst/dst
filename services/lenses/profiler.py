"""Sampling-pass orchestrator: learn what undocumented columns contain.

`run_sampling_pass` reads the stored catalog profiles (the catalog pass is the
prerequisite — it supplies column names/types and the gap picture), finds the
columns the catalog couldn't explain — no description and no catalog-native
stats (Postgres ``pg_stats`` freebies and the like) — and asks the connector's
`SamplingProfiler` capability to sample exactly those, plus one
MAX(partition/best time column) probe per table for logical freshness. Results
merge into the stored profile (catalog facts always win; sampling only fills
gaps) and upsert via profile_store with ``source="sampled"``/``"mixed"``.

A column named in ``exclude_columns`` ("column" or "table.column") is sampled
shape-only — null rate and cardinality, never literal values — and is never used
for the freshness probe. The exclusion is applied here *and* honoured inside the
connectors (defense in depth), and the merge strips literals once more, so
nothing shape-only can reach the store even from a misbehaving connector.
Profiling never runs inside a query (refresh is trigger/staleness work).

`profile_connection` is the same two passes with no store behind them — the
file-first CLI path (`dst introspect --profile`) has no session to read
stored profiles back from.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    ColumnProfile,
    ColumnSampleSpec,
    TableProfile,
    TableSampleSpec,
    is_temporal_type,
)
from services.contracts.protocols import Connector, SamplingProfiler
from services.lenses import profile_store
from services.lenses.profiler_catalog import catalog_profiles

# Freshness-probe column preference, most trustworthy first: a load/update stamp
# beats a creation stamp beats any other time column.
_FRESHNESS_NAME_HINTS = ("updated", "modified", "loaded", "ingested", "created", "inserted")


def run_sampling_pass(
    session: Session,
    connector: Connector,
    connection_name: str,
    tables: list[str] | None = None,
    exclude_columns: list[str] | None = None,
) -> list[TableProfile]:
    """Sample the catalog pass's gaps on ``connection_name``, merge + upsert, return profiles.

    Only tables with a stored profile are considered (run the catalog pass
    first); ``tables`` narrows the scope, ``exclude_columns`` ("column" or
    "table.column") force shape-only per call. A connector without the
    `SamplingProfiler` capability leaves the stored profiles untouched. Returns
    every in-scope profile, updated where sampling happened.
    """
    stored = profile_store.list_profiles(session, connection_name)
    if tables is not None:
        wanted = set(tables)
        stored = [s for s in stored if s.profile.table in wanted]
    profiles: dict[str, TableProfile] = {s.profile.table: s.profile for s in stored}
    excluded = frozenset(exclude_columns or [])
    for table, merged in sample_profiles(connector, list(profiles.values()), excluded).items():
        profile_store.upsert_profile(session, merged)
        profiles[table] = merged
    return list(profiles.values())


def sample_profiles(
    connector: Connector,
    profiles: list[TableProfile],
    excluded: frozenset[str] = frozenset(),
) -> dict[str, TableProfile]:
    """The sampling pass without the store: sample each profile's gaps, return the
    merged profiles keyed by table — only the tables the connector actually sampled."""
    if not profiles or not isinstance(connector, SamplingProfiler):
        return {}
    specs = [spec for p in profiles if (spec := build_sample_spec(p, excluded)) is not None]
    if not specs:
        return {}
    base_by_table = {p.table: p for p in profiles}
    merged: dict[str, TableProfile] = {}
    for fragment in connector.sample_profile(
        specs, max_rows=SAMPLE_MAX_ROWS, max_distinct_for_enum=LOW_CARDINALITY_MAX
    ):
        base = base_by_table.get(fragment.table)
        if base is None:  # a table we never asked about — refuse to store it
            continue
        merged[fragment.table] = merge_sampled(base, fragment, excluded)
    return merged


def profile_connection(
    connector: Connector, connection: str, tables: list[str] | None = None
) -> list[TableProfile]:
    """Catalog pass + sampling pass in one call, against the warehouse only.

    What `dst introspect --profile` runs. The stored-profile path needs a
    server, an apply and a background pass to have finished; the file-first path
    — the one every scaffolded project takes, because it declares its connection
    in dst.yaml — has none of those, and printed schema with no facts forever.
    """
    profiles = catalog_profiles(connector, connection)
    if tables:
        wanted = set(tables)
        profiles = [p for p in profiles if p.table in wanted]
    merged = sample_profiles(connector, profiles)
    return [merged.get(p.table, p) for p in profiles]


def build_sample_spec(
    profile: TableProfile, excluded: frozenset[str] | None = None
) -> TableSampleSpec | None:
    """The sampling worklist for one stored profile: gap columns + a freshness column.

    A *gap* is a column the catalog pass left unexplained — no description and no
    catalog-native stats (``null_rate``/``top_values``). Returns None when the
    table needs nothing (fully documented and no time column to probe).
    """
    excluded = excluded if excluded is not None else frozenset()
    columns = [
        ColumnSampleSpec(
            name=c.name, type=c.type, shape_only=_shape_only(profile.table, c.name, excluded)
        )
        for c in profile.columns
        if _is_gap(c)
    ]
    freshness = _freshness_column(profile, excluded)
    if not columns and freshness is None:
        return None
    return TableSampleSpec(
        table=profile.table,
        columns=columns,
        row_count=profile.row_count,
        freshness_column=freshness,
        is_view=profile.is_view,
    )


def merge_sampled(
    base: TableProfile, fragment: TableProfile, excluded: frozenset[str] | None = None
) -> TableProfile:
    """Fold a connector's sampled fragment into the stored profile.

    Catalog facts win — descriptions, row counts, partitioning are never
    overwritten; sampling only fills blanks. Literal values are stripped once
    more for shape-only columns: the merge is the last gate before persistence.
    """
    excluded = excluded if excluded is not None else frozenset()
    sampled_by_name = {c.name: c for c in fragment.columns}
    columns: list[ColumnProfile] = []
    for column in base.columns:
        sampled = sampled_by_name.get(column.name)
        if sampled is None:
            columns.append(column)
            continue
        update: dict[str, object] = {}
        if sampled.null_rate is not None:
            update["null_rate"] = sampled.null_rate
        # An exact count is never replaced by an estimate. `dst introspect`
        # without `--profile` already carries DuckDB's real COUNT(DISTINCT), and
        # overwriting 12,333 (a key) with a sampled 11,076 made the more thorough
        # mode the less accurate one.
        if sampled.distinct_count is not None and (
            sampled.distinct_is_exact or not column.distinct_is_exact
        ):
            update["distinct_count"] = sampled.distinct_count
            update["distinct_is_exact"] = sampled.distinct_is_exact
            update["is_low_cardinality"] = sampled.is_low_cardinality
        if sampled.top_values is not None:
            update["top_values"] = sampled.top_values
            update["values_complete"] = sampled.values_complete
        if sampled.min is not None:
            update["min"] = sampled.min
        if sampled.max is not None:
            update["max"] = sampled.max
        merged = column.model_copy(update=update)
        if _shape_only(base.table, merged.name, excluded):  # the last gate before persistence
            merged = merged.model_copy(update={"top_values": None, "min": None, "max": None})
        columns.append(merged)
    return base.model_copy(
        update={
            "columns": columns,
            "last_updated_logical": fragment.last_updated_logical or base.last_updated_logical,
            "profiled_at": fragment.profiled_at,
            "sampled_rows": fragment.sampled_rows,
            "source": _merged_source(base),
        }
    )


def _is_gap(column: ColumnProfile) -> bool:
    """True when the catalog pass left this column unexplained."""
    return column.description is None and column.null_rate is None and column.top_values is None


def _shape_only(table: str, column: str, excluded: frozenset[str]) -> bool:
    """Is this column excluded from literal collection? Bare name or table-qualified."""
    return column in excluded or f"{table}.{column}" in excluded


def _freshness_column(profile: TableProfile, excluded: frozenset[str]) -> str | None:
    """The partition column if usable, else the most freshness-like time-typed column."""
    by_name = {c.name: c for c in profile.columns}
    partition = profile.partitioning.column if profile.partitioning else None
    if partition is not None:
        column = by_name.get(partition)  # ingestion-time pseudo-columns aren't probeable
        if column is not None and not _shape_only(profile.table, column.name, excluded):
            return partition
    candidates = [
        c
        for c in profile.columns
        if is_temporal_type(c.type) and not _shape_only(profile.table, c.name, excluded)
    ]
    if not candidates:
        return None

    def score(column: ColumnProfile) -> int:
        name = column.name.lower()
        for rank, hint in enumerate(_FRESHNESS_NAME_HINTS):
            if hint in name:
                return len(_FRESHNESS_NAME_HINTS) - rank
        return 0

    return max(candidates, key=score).name  # max is stable: ties keep column order


def _merged_source(base: TableProfile) -> Literal["sampled", "mixed"]:
    """Pick "mixed" when the catalog contributed real knowledge, "sampled" when it had none."""
    if base.source != "catalog":
        return base.source  # re-sampling keeps the established provenance
    catalog_knew = base.description is not None or any(
        c.description is not None or c.null_rate is not None or c.top_values is not None
        for c in base.columns
    )
    return "mixed" if catalog_knew else "sampled"
