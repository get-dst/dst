"""Table-profiling contracts — the persisted physical truth of a table.

`TableProfile`/`ColumnProfile` are the *persistence* shape the app reads (stored per
connection by `services/lenses/profile_store.py`); `ColumnSchema.profile` on the
warehouse transport stays what a connector returns from introspection. `JoinCandidate`
carries an inferred join key awaiting human approval.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from services.contracts.warehouse import SchemaSnapshot, TableSchema

# A column with at most this many distinct values is "low cardinality" (enum-like) —
# the threshold the sampling pass uses to collect literals into top_values.
LOW_CARDINALITY_MAX = 25

# Hard caps for the sampling pass: at most this many rows are ever pulled
# into one sampling query, and on engines that bill scanned bytes (BigQuery) a query
# whose dry-run estimate exceeds the bytes budget is refused, never run.
SAMPLE_MAX_ROWS = 10_000
SAMPLE_BYTES_BUDGET = 2_000_000_000  # ~2 GB

# Above this many rows the pass samples; at or below it, it counts EXACTLY.
#
# `SAMPLE_MAX_ROWS` conflated two different things — rows *transferred* and rows
# *scanned*. The stats query is an aggregate: it returns ONE row whatever the
# table holds, so capping it at 10k buys nothing in transfer and costs all the
# accuracy — `approx_count_distinct` over a random sample can report more
# distinct ids than a small table has rows, and can drop values out of an enum
# column entirely. The only real cost of exactness is the scan, which grows with
# the table while the sampled scan stays flat; below roughly a million rows that
# difference is a fraction of a second on an embedded engine and a small scan on
# a billed warehouse. Above the cap every derived number is an estimate and is
# labelled as one, everywhere it is printed. An unknown row count counts exactly
# too: the sampled path already reads the whole relation in that case
# (`_sample_percent` returns 100), so it was paying the scan and throwing away
# the accuracy.
EXACT_PROFILE_MAX_ROWS = 1_000_000


class PartitioningProfile(BaseModel):
    """How a table is partitioned, and how far its partitions currently go."""

    column: str | None = None  # None for ingestion-time partitioning
    kind: str | None = None  # "time" | "ingestion" | "range" | "list" | "hash"
    latest_partition: str | None = None  # latest partition id — a logical-freshness hint


class ColumnProfile(BaseModel):
    name: str
    type: str
    nullable: bool = True
    description: str | None = None
    description_source: Literal["warehouse", "dbt", "sampled", "llm"] | None = None
    null_rate: float | None = None
    distinct_count: int | None = None
    # Whether `distinct_count` is COUNT(DISTINCT …) over every row (True) or a
    # count observed in a random sample (False). False makes it a LOWER BOUND on
    # the column's real cardinality, never an equality — rendering it as a bare
    # integer is what let "distinct: 520" sit under "(~500 rows)". Defaults to
    # False so a profile stored before this field existed reads as an estimate.
    distinct_is_exact: bool = False
    is_low_cardinality: bool = False
    top_values: list[str] | None = None  # actual literals, low-cardinality columns only
    # Whether `top_values` is EVERY value the column holds. A value dictionary is
    # where the business meaning lives, so a partial one must say so: silently
    # dropping a couple of elements is worse than printing none, because the
    # author treats the list as complete. True only when the values were read from the
    # whole table and their count matches an exact `distinct_count`.
    values_complete: bool = False
    min: str | None = None
    max: str | None = None
    # The value's SHAPE when a textual column hides structure — JSON
    # objects, '(lon,lat)' points, numbers-as-text — plus the access pattern a
    # query needs. Without it a lens compares raw JSON to a plain string and
    # concludes the data isn't there.
    value_shape: (
        Literal["json-object", "json-array", "point", "numeric-text", "date-text"] | None
    ) = None
    access_hint: str | None = None


class TableProfile(BaseModel):
    connection: str  # registered connection name (the store key)
    table: str  # named exactly as introspect() names it (e.g. "schema.table")
    description: str | None = None
    row_count: int | None = None
    is_view: bool | None = None  # None = the catalog did not say
    # Rows the sampling pass actually read when it could not read them all —
    # None means every row was counted, so the column facts below are exact.
    # It is the number that puts the sampling boundary on screen.
    sampled_rows: int | None = None
    partitioning: PartitioningProfile | None = None
    clustering: list[str] = Field(default_factory=list)
    last_updated_physical: datetime | None = None  # engine timestamp; best-effort
    last_updated_logical: datetime | None = None  # max of a real time column
    profiled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["catalog", "sampled", "mixed"] = "catalog"
    columns: list[ColumnProfile] = Field(default_factory=list)


class ColumnSampleSpec(BaseModel):
    """One column the sampling pass should profile — a catalog-pass gap.

    ``shape_only`` narrows what is collected: the column still gets shape stats
    (null rate, cardinality) but its literal values — top_values *and* min/max —
    are never read. It is how ``exclude_columns`` reaches the warehouse.
    """

    name: str
    type: str = ""  # warehouse type from the catalog pass; decides min/max eligibility
    shape_only: bool = False


class TableSampleSpec(BaseModel):
    """What `SamplingProfiler.sample_profile` should collect for one table.

    Built by the orchestrator from the stored catalog profile: only gap columns,
    the catalog's row-count estimate (sizes percent-based TABLESAMPLE), and the
    column the MAX() logical-freshness probe should read.
    """

    table: str  # named exactly as the stored profile / introspect() names it
    columns: list[ColumnSampleSpec] = Field(default_factory=list)
    row_count: int | None = None
    freshness_column: str | None = None
    # Views can't take block-level TABLESAMPLE on BigQuery/Postgres — the
    # builders downgrade them to a plain LIMIT sample.
    is_view: bool | None = None


class JoinCandidate(BaseModel):
    """An inferred join key between two tables, awaiting approval.

    ``id`` is store-assigned (None until persisted); the candidate is scoped to a
    connection by the store, like a `TableProfile`.
    """

    id: str | None = None
    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    evidence: Literal["fk", "dbt", "name+overlap"]
    overlap_ratio: float | None = None
    status: Literal["candidate", "approved", "rejected"] = "candidate"


def profile_from_table_schema(
    table: TableSchema, *, connection: str, profiled_at: datetime | None = None
) -> TableProfile:
    """Derive a basic `TableProfile` from one introspected `TableSchema`.

    Carries over what the transport already holds (names/types/descriptions/row
    count/partitioning/clustering and any per-column ``profile`` dict) so every
    connector gets a catalog profile for free, without engine-specific work.
    """
    columns: list[ColumnProfile] = []
    for col in table.columns:
        raw_distinct = (col.profile or {}).get("distinct")
        distinct = raw_distinct if isinstance(raw_distinct, int) else None
        # A connector says so when its introspection counted rather than estimated
        # (DuckDB runs a real COUNT(DISTINCT) per column; Postgres' pg_stats
        # n_distinct is an estimate) — absent the claim, it is an estimate.
        exact = bool((col.profile or {}).get("distinct_exact"))
        columns.append(
            ColumnProfile(
                name=col.name,
                type=col.type,
                nullable=col.nullable,
                description=col.description,
                description_source="warehouse" if col.description else None,
                distinct_count=distinct,
                distinct_is_exact=exact and distinct is not None,
                is_low_cardinality=distinct is not None and 0 < distinct <= LOW_CARDINALITY_MAX,
            )
        )
    partitioning = PartitioningProfile(column=table.partitioning) if table.partitioning else None
    return TableProfile(
        connection=connection,
        table=table.name,
        description=table.description,
        row_count=table.row_count,
        is_view=table.is_view,
        partitioning=partitioning,
        clustering=list(table.clustering),
        profiled_at=profiled_at or datetime.now(UTC),
        source="catalog",
        columns=columns,
    )


def profiles_from_snapshot(
    snapshot: SchemaSnapshot, *, connection: str | None = None
) -> list[TableProfile]:
    """Derive basic `TableProfile`s from a full introspection snapshot.

    The fallback path for connectors without the `CatalogProfiler` capability,
    and the conversion the catalog endpoints persist on a live introspection.
    """
    now = datetime.now(UTC)
    label = connection or snapshot.connection
    return [
        profile_from_table_schema(t, connection=label, profiled_at=now) for t in snapshot.tables
    ]


# ── type-shape helpers ───────────────────────────────────────────────────────

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

_NUMERIC_TYPE_TOKENS = frozenset(
    {
        "int",
        "integer",
        "bigint",
        "smallint",
        "tinyint",
        "hugeint",
        "int2",
        "int4",
        "int8",
        "int16",
        "int32",
        "int64",
        "decimal",
        "numeric",
        "number",
        "double",
        "float",
        "real",
        "float4",
        "float8",
        "float32",
        "float64",
    }
)
_TEMPORAL_TYPE_TOKENS = frozenset(
    {"date", "datetime", "time", "timetz", "timestamp", "timestamptz"}
)


# Compound types whose *members* are typed — tokenising them finds the member's
# type and answers about that, not about the column. STRUCT(a INTEGER, b VARCHAR)
# is not a number, and MIN/MAX over one is not a range an author can use.
_COMPOUND_TYPE_PREFIXES = ("STRUCT", "MAP", "ARRAY", "LIST", "ROW", "RECORD")


def _type_tokens(type_name: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(type_name.lower()) if t]


def is_compound_type(type_name: str) -> bool:
    """True for a container type — STRUCT/MAP/ARRAY/RECORD or a ``T[]`` list."""
    base = type_name.strip().upper()
    return base.endswith("[]") or base.startswith(_COMPOUND_TYPE_PREFIXES)


def is_numeric_type(type_name: str) -> bool:
    """True for warehouse numeric types, across dialect spellings (INT64, NUMBER(38,0), …)."""
    if is_compound_type(type_name):
        return False
    return any(t in _NUMERIC_TYPE_TOKENS for t in _type_tokens(type_name))


def is_temporal_type(type_name: str) -> bool:
    """True for warehouse date/time types (DATE, TIMESTAMP_NTZ, timestamp with time zone, …)."""
    if is_compound_type(type_name):
        return False
    return any(t in _TEMPORAL_TYPE_TOKENS for t in _type_tokens(type_name))
