"""Shared machinery for the guarded profiling pass.

Every `SamplingProfiler` connector delegates here: `sample_tables` runs, per
table, a two-phase read — one aggregate query for null rates / cardinality /
min-max, then one tiny top-k query per low-cardinality column — plus a
full-table ``MAX(time column)`` probe for logical freshness, all through the
connector's own read-only query path. Only the FROM clause differs per dialect
(``USING SAMPLE`` / ``TABLESAMPLE SYSTEM`` / ``SAMPLE (n ROWS)`` / plain
``LIMIT``) — see `_sampled_relation`.

**Exact or labelled, never quietly approximate.** A table at or below
`EXACT_PROFILE_MAX_ROWS` (or whose row count the catalog does not know) is read
WHOLE, with `COUNT(DISTINCT …)`: the aggregate returns one row whatever the
table holds, so the cap on rows *transferred* was buying nothing while the
approximate aggregate over a random sample reported more distinct values than
there were rows and dropped rare enum members. Above the threshold the pass
samples, and everything it derives is marked as an estimate — `distinct_count`
becomes a LOWER BOUND (`distinct_is_exact=False`), a value list that may be
missing members is `values_complete=False`, and `TableProfile.sampled_rows`
records how many rows were actually read. Nothing downstream may print an
estimate as a fact.

``shape_only`` is honoured at this level, not just by the caller that built the
spec: such a column never has literal values collected (no MIN/MAX, no
top_values, no freshness probe) — shape only. A runner may raise
`SampleBudgetExceeded` to refuse a query
(BigQuery's dry-run bytes-gate); a refused stats query skips the whole table so
the stored catalog profile stands.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from services.contracts.profile import (
    EXACT_PROFILE_MAX_ROWS,
    ColumnProfile,
    ColumnSampleSpec,
    TableProfile,
    TableSampleSpec,
    is_numeric_type,
    is_temporal_type,
)
from services.contracts.warehouse import QueryResult

SamplingDialect = Literal["duckdb", "postgres", "bigquery", "snowflake", "mysql"]

# Executes one sampling query read-only and returns its result. Connectors pass
# their own `execute`; BigQuery passes a dry-run-gated wrapper instead.
RunQuery = Callable[[str], QueryResult]

_APPROX_DISTINCT: dict[str, str] = {
    "duckdb": "approx_count_distinct",
    "bigquery": "APPROX_COUNT_DISTINCT",
    "snowflake": "APPROX_COUNT_DISTINCT",
    # postgres / mysql: no approximate aggregate — exact COUNT(DISTINCT …) on the sample
}


class SampleBudgetExceeded(RuntimeError):
    """A runner refused a sampling query whose estimated cost exceeds its bytes budget."""


# The per-table skip channel. `dst probe` mirrors this logger to stderr so a
# skipped table is visible where the operator is, not only in a server log.
logger = logging.getLogger(__name__)


def sample_tables(
    run: RunQuery,
    tables: list[TableSampleSpec],
    *,
    connection: str,
    dialect: SamplingDialect,
    max_rows: int,
    max_distinct_for_enum: int,
    exact_max_rows: int = EXACT_PROFILE_MAX_ROWS,
) -> list[TableProfile]:
    """Run the guarded profiling pass for every spec; a table that cannot sample
    is SKIPPED with a warning, never allowed to abort the connection.

    One view whose sampling query failed dry run used to kill the whole
    connection's probe — every table-backed entity lost its dictionaries with
    it. The catalog profile stands for a skipped table.
    """
    profiles: list[TableProfile] = []
    for i, spec in enumerate(tables, start=1):
        # Progress at INFO, per table: a silent `dst probe` makes "profiling 70
        # entities, this takes a while" indistinguishable from "hung an hour
        # ago", and that ambiguity is itself a defect. The CLI mirrors this
        # logger.
        logger.info("sampling %s (%d/%d)", spec.table, i, len(tables))
        try:
            profile = sample_table(
                run,
                spec,
                connection=connection,
                dialect=dialect,
                max_rows=max_rows,
                max_distinct_for_enum=max_distinct_for_enum,
                exact_max_rows=exact_max_rows,
            )
        except Exception as exc:  # noqa: BLE001 — one table's failure is its own
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            logger.warning(
                "table %s not sampled (%s) — its catalog profile stands", spec.table, first
            )
            continue
        if profile is not None:
            profiles.append(profile)
    return profiles


def counts_exactly(row_count: int | None, exact_max_rows: int) -> bool:
    """Whether this table's facts are counted rather than estimated.

    An unknown row count counts exactly: the sampled path reads the whole
    relation anyway when it cannot size it (`_sample_percent` → 100), so it was
    already paying for the scan and only throwing away the accuracy.
    """
    return row_count is None or row_count <= exact_max_rows


def sample_table(
    run: RunQuery,
    spec: TableSampleSpec,
    *,
    connection: str,
    dialect: SamplingDialect,
    max_rows: int,
    max_distinct_for_enum: int,
    exact_max_rows: int = EXACT_PROFILE_MAX_ROWS,
) -> TableProfile | None:
    """Profile one table per its spec; None when there is nothing to do (or no budget)."""
    specs = spec.columns
    exact = counts_exactly(spec.row_count, exact_max_rows)
    columns: list[ColumnProfile] = []
    scanned = 0
    if specs:
        try:
            exact, result = _run_stats(
                run, spec, specs, dialect=dialect, max_rows=max_rows, exact=exact
            )
        except SampleBudgetExceeded as exc:
            # Too big to read within budget — the catalog profile stands. Said
            # out loud: a silent cap reads as coverage.
            logger.warning("table %s not sampled (%s)", spec.table, exc)
            return None
        columns = _fold_stats(
            specs, result, exact=exact, max_distinct_for_enum=max_distinct_for_enum
        )
        scanned = _scanned_rows(result)
        if columns:
            _collect_top_values(
                run,
                spec,
                specs,
                columns,
                dialect=dialect,
                max_rows=max_rows,
                limit=max_distinct_for_enum,
                exact=exact,
            )
            _collect_value_shapes(
                run, spec, specs, columns, dialect=dialect, max_rows=max_rows, exact=exact
            )
    last_logical: datetime | None = None
    if spec.freshness_column is not None and not _freshness_blocked(spec.freshness_column, specs):
        last_logical = _probe_freshness(run, spec.table, spec.freshness_column, dialect=dialect)
    if not columns and last_logical is None:
        return None
    return TableProfile(
        connection=connection,
        table=spec.table,
        last_updated_logical=last_logical,
        source="sampled",
        # None means "every row was counted" and nothing else may set it — a
        # sample that happened to return 0 rows (block-level TABLESAMPLE on a
        # tiny percent) must not read as exactness.
        sampled_rows=None if exact else scanned,
        columns=columns,
    )


def _run_stats(
    run: RunQuery,
    spec: TableSampleSpec,
    specs: list[ColumnSampleSpec],
    *,
    dialect: SamplingDialect,
    max_rows: int,
    exact: bool,
) -> tuple[bool, QueryResult]:
    """The stats query, downgrading to a labelled sample when exactness is refused.

    A budget runner (BigQuery's dry-run bytes gate) can refuse the whole-table
    read on a table small enough by rows but wide enough by bytes. Falling back
    to the capped sample keeps a labelled estimate where the old code dropped
    the table entirely — the caller learns which it got from the returned flag.
    """
    sql = stats_sql(
        spec.table,
        specs,
        dialect=dialect,
        max_rows=max_rows,
        row_count=spec.row_count,
        exact=exact,
        is_view=bool(spec.is_view),
    )
    assert sql is not None  # specs is non-empty at the one call site
    if not exact:
        return False, run(sql)
    try:
        return True, run(sql)
    except SampleBudgetExceeded:
        sampled = stats_sql(
            spec.table,
            specs,
            dialect=dialect,
            max_rows=max_rows,
            row_count=spec.row_count,
            exact=False,
            is_view=bool(spec.is_view),
        )
        assert sampled is not None
        return False, run(sampled)


# ── SQL builders (dialect-correct, capped) ───────────────────────────────────


def stats_sql(
    table: str,
    columns: list[ColumnSampleSpec],
    *,
    dialect: SamplingDialect,
    max_rows: int,
    row_count: int | None,
    exact: bool = False,
    is_view: bool = False,
) -> str | None:
    """One aggregate query: per column COUNT (nulls), distinct count, and MIN/MAX
    for time + numeric columns — never MIN/MAX for a shape-only column (those are
    literals too), and never for a compound type (MIN over a STRUCT is not a range).

    ``exact`` reads the whole table with `COUNT(DISTINCT …)`; otherwise the read
    is a capped sample and the dialect's approximate aggregate is used where it
    has one."""
    if not columns:
        return None
    approx = None if exact else _APPROX_DISTINCT.get(dialect)
    selects = ["COUNT(*) AS n_rows"]
    for i, col in enumerate(columns):
        qc = _quote_name(col.name, dialect)
        selects.append(f"COUNT({qc}) AS nn_{i}")
        if approx is not None:
            selects.append(f"{approx}({qc}) AS dc_{i}")
        else:
            selects.append(f"COUNT(DISTINCT {qc}) AS dc_{i}")
        if not col.shape_only and (is_numeric_type(col.type) or is_temporal_type(col.type)):
            selects.append(f"MIN({qc}) AS mn_{i}")
            selects.append(f"MAX({qc}) AS mx_{i}")
    select_list = ", ".join(_quote_name(c.name, dialect) for c in columns)
    relation = _sampled_relation(
        table,
        select_list,
        dialect=dialect,
        max_rows=max_rows,
        row_count=row_count,
        exact=exact,
        is_view=is_view,
    )
    return f"SELECT {', '.join(selects)} FROM {relation}"


def top_values_sql(
    table: str,
    column: str,
    *,
    dialect: SamplingDialect,
    max_rows: int,
    row_count: int | None,
    limit: int,
    exact: bool = False,
    is_view: bool = False,
) -> str:
    """The value-dictionary query: top-k literals by frequency.

    ``exact`` reads every row, so the list it returns is every value the column
    holds (up to ``limit``); otherwise it is the sample's values and the caller
    must mark it incomplete."""
    qc = _quote_name(column, dialect)
    relation = _sampled_relation(
        table,
        qc,
        dialect=dialect,
        max_rows=max_rows,
        row_count=row_count,
        exact=exact,
        is_view=is_view,
    )
    return (
        f"SELECT {qc} AS v, COUNT(*) AS n FROM {relation} "
        f"WHERE {qc} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(limit)}"
    )


def freshness_sql(table: str, column: str, *, dialect: SamplingDialect) -> str:
    """The logical-freshness probe: full-table MAX of the partition/best time column."""
    return f"SELECT MAX({_quote_name(column, dialect)}) AS v FROM {_quote_table(table, dialect)}"


def _sampled_relation(
    table: str,
    select_list: str,
    *,
    dialect: SamplingDialect,
    max_rows: int,
    row_count: int | None,
    exact: bool = False,
    is_view: bool = False,
) -> str:
    """The dialect's capped sample as a derived table — or the table itself.

    ``exact`` means no sample clause at all: the aggregates read every row, and
    the query still returns one row, so the sample bought only inaccuracy.
    Otherwise, row-based clauses take ``max_rows`` directly; percent-based ones
    (Postgres, BigQuery) derive the percent from the catalog's row-count estimate
    and keep a hard ``LIMIT`` as the cap.

    ``is_view`` swaps block-level TABLESAMPLE for a plain ``LIMIT`` where
    TABLESAMPLE is a base-table-only construct (BigQuery, Postgres): one view
    used to abort the whole connection's probe. A LIMIT
    read is a biased sample — everything derived from one is already labelled
    an estimate.
    """
    qt = _quote_table(table, dialect)
    if exact:
        return qt
    rows = int(max_rows)
    if dialect == "duckdb":
        inner = f"SELECT {select_list} FROM {qt} USING SAMPLE {rows} ROWS"
    elif dialect == "snowflake":
        inner = f"SELECT {select_list} FROM {qt} SAMPLE ({rows} ROWS)"
    elif dialect == "postgres":
        if is_view:
            inner = f"SELECT {select_list} FROM {qt} LIMIT {rows}"
        else:
            percent = _sample_percent(max_rows, row_count)
            inner = f"SELECT {select_list} FROM {qt} TABLESAMPLE SYSTEM ({percent}) LIMIT {rows}"
    elif dialect == "bigquery":
        if is_view:
            inner = f"SELECT {select_list} FROM {qt} LIMIT {rows}"
        else:
            percent = _sample_percent(max_rows, row_count)
            inner = (
                f"SELECT {select_list} FROM {qt} TABLESAMPLE SYSTEM ({percent} PERCENT) "
                f"LIMIT {rows}"
            )
    else:  # mysql — no TABLESAMPLE; an unordered LIMIT is the sample
        inner = f"SELECT {select_list} FROM {qt} LIMIT {rows}"
    return f"({inner}) AS _dst_sample"


def _sample_percent(max_rows: int, row_count: int | None) -> float:
    if not row_count or row_count <= max_rows:
        return 100.0
    return max(0.01, min(100.0, round(100.0 * max_rows / row_count, 4)))


def _quote_table(table: str, dialect: SamplingDialect) -> str:
    if dialect == "bigquery":
        return f"`{table}`"  # one backtick pair around the dotted path, as BQ prefers
    return ".".join(_quote_name(part, dialect) for part in table.split("."))


def _quote_name(name: str, dialect: SamplingDialect) -> str:
    quote = "`" if dialect in ("mysql", "bigquery") else '"'
    return f"{quote}{name.replace(quote, quote * 2)}{quote}"


# ── result folding ───────────────────────────────────────────────────────────


def _scanned_rows(result: QueryResult) -> int:
    """How many rows the stats query actually read."""
    if not result.rows:
        return 0
    idx = {str(name).lower(): i for i, name in enumerate(result.columns)}
    return _as_int(result.rows[0][idx["n_rows"]]) or 0


def _fold_stats(
    columns: list[ColumnSampleSpec],
    result: QueryResult,
    *,
    exact: bool,
    max_distinct_for_enum: int,
) -> list[ColumnProfile]:
    if not result.rows:
        return []
    row = result.rows[0]
    # Aliases come back lower/upper-cased per dialect (Snowflake upcases) — fold case.
    idx = {str(name).lower(): i for i, name in enumerate(result.columns)}
    scanned = _as_int(row[idx["n_rows"]]) or 0
    profiles: list[ColumnProfile] = []
    for i, spec in enumerate(columns):
        non_null = _as_int(row[idx[f"nn_{i}"]])
        distinct = _as_int(row[idx[f"dc_{i}"]])
        # An estimate that exceeds the rows it was computed over is self-evidently
        # broken — approx_count_distinct claimed 520 distinct ids in 500 rows.
        # Sampled, the clamped number is a lower bound on the table's cardinality.
        if distinct is not None and scanned > 0:
            distinct = min(distinct, scanned)
        null_rate = None
        if scanned > 0 and non_null is not None:
            null_rate = round(1.0 - (non_null / scanned), 6)
        minimum = row[idx[f"mn_{i}"]] if f"mn_{i}" in idx else None
        maximum = row[idx[f"mx_{i}"]] if f"mx_{i}" in idx else None
        profiles.append(
            ColumnProfile(
                name=spec.name,
                type=spec.type,
                null_rate=null_rate,
                distinct_count=distinct,
                distinct_is_exact=exact and distinct is not None,
                is_low_cardinality=distinct is not None and 0 < distinct <= max_distinct_for_enum,
                min=_literal(minimum) if minimum is not None else None,
                max=_literal(maximum) if maximum is not None else None,
            )
        )
    return profiles


def _literal(value: object) -> str:
    """A value rendered as SQL sees it, not as Python repr()s it.

    ``str(True)`` is ``'True'``, which is not a boolean literal in any dialect,
    and ``str(['t1','t2'])`` is a Python list repr — both were printed to authors
    as though they could be pasted into a WHERE clause.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | dict):
        try:
            return json.dumps(value, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR")
_POINT_RE = re.compile(r"^\(\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*\)$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?)?$")


def classify_value_shape(name: str, samples: list[str]) -> tuple[str, str] | None:
    """(value_shape, access_hint) for textual values hiding structure — or None.

    Pure and conservative: EVERY non-empty sample must match a shape before we
    claim it; a wrong hint plants the trap this exists to remove."""
    vals = [s.strip() for s in samples if s and s.strip()]
    if len(vals) < 2:
        return None
    if all(v.startswith("{") and v.endswith("}") for v in vals):
        keys: set[str] = set()
        for v in vals:
            try:
                parsed = json.loads(v)
            except ValueError:
                return None
            if not isinstance(parsed, dict):
                return None
            keys.update(parsed)
        shown = ", ".join(sorted(keys)[:4])
        return (
            "json-object",
            f"JSON object in text (keys: {shown}) — access via "
            f"json_extract_string({name}, '$.<key>'); never compare the raw column",
        )
    if all(v.startswith("[") and v.endswith("]") for v in vals):
        return ("json-array", f"JSON array in text — unnest/json functions on {name}")
    if all(_POINT_RE.match(v) for v in vals):
        return (
            "point",
            f"'(x,y)' point string — x = CAST(split_part(trim({name}, '()'), ',', 1) AS "
            f"DOUBLE), y = part 2",
        )
    if all(_NUM_RE.match(v) for v in vals):
        return ("numeric-text", f"numeric stored as text — CAST({name} AS DOUBLE) before math")
    if all(_DATE_RE.match(v) for v in vals):
        return ("date-text", f"date/timestamp stored as text — CAST({name} AS TIMESTAMP)")
    return None


def _collect_value_shapes(
    run: RunQuery,
    spec: TableSampleSpec,
    specs: list[ColumnSampleSpec],
    columns: list[ColumnProfile],
    *,
    dialect: SamplingDialect,
    max_rows: int,
    exact: bool,
) -> None:
    """Classify textual columns' value shapes — never for shape-only columns,
    whose literals are not read at all. Low-cardinality columns classify from
    already-collected top_values (no extra query); the rest get one 3-row probe
    each."""
    for col_spec, col in zip(specs, columns, strict=True):
        if col_spec.shape_only or not col_spec.type.upper().startswith(_TEXT_TYPES):
            continue
        samples = list(col.top_values or [])
        if not samples:
            sql = shape_probe_sql(
                spec.table,
                col_spec.name,
                dialect=dialect,
                max_rows=max_rows,
                row_count=spec.row_count,
                exact=exact,
                is_view=bool(spec.is_view),
            )
            try:
                samples = [str(r[0]) for r in run(sql).rows]
            except SampleBudgetExceeded:
                continue
        shape = classify_value_shape(col_spec.name, samples)
        if shape is not None:
            col.value_shape, col.access_hint = shape  # type: ignore[assignment]


def shape_probe_sql(
    table: str,
    column: str,
    *,
    dialect: SamplingDialect,
    max_rows: int,
    row_count: int | None,
    exact: bool = False,
    is_view: bool = False,
) -> str:
    """Three non-null examples from the relation — shape classification only."""
    q = _quote_name(column, dialect)
    rel = _sampled_relation(
        table,
        q,
        dialect=dialect,
        max_rows=max_rows,
        row_count=row_count,
        exact=exact,
        is_view=is_view,
    )
    return f"SELECT {q} FROM {rel} WHERE {q} IS NOT NULL LIMIT 3"


def _collect_top_values(
    run: RunQuery,
    spec: TableSampleSpec,
    specs: list[ColumnSampleSpec],
    columns: list[ColumnProfile],
    *,
    dialect: SamplingDialect,
    max_rows: int,
    limit: int,
    exact: bool,
) -> None:
    """Fill ``top_values`` for low-cardinality columns — never for shape-only ones.

    The list is COMPLETE only when it was read from the whole table and its
    length matches an exact distinct count; a list that came out of a sample, or
    that filled the ``limit``, is marked incomplete. `distinct_count` is then
    reconciled with the list so the two can never contradict each other: 19
    values alongside ``distinct_count: 18`` was an impossible pair printed as
    fact, because the approximate aggregate and the top-k query disagreed.
    """
    for col_spec, col in zip(specs, columns, strict=True):
        if col_spec.shape_only or not col.is_low_cardinality:
            continue
        sql = top_values_sql(
            spec.table,
            col_spec.name,
            dialect=dialect,
            max_rows=max_rows,
            row_count=spec.row_count,
            limit=limit,
            exact=exact,
            is_view=bool(spec.is_view),
        )
        try:
            rows = run(sql).rows
        except SampleBudgetExceeded:
            continue
        values = [_literal(r[0]) for r in rows]
        col.top_values = values or None
        if not values:
            continue
        if exact and col.distinct_count is not None and len(values) == col.distinct_count:
            col.values_complete = True
        elif col.distinct_count is not None:
            # The values are direct evidence of at least this many distinct ones.
            col.distinct_count = max(col.distinct_count, len(values))


def _probe_freshness(
    run: RunQuery, table: str, column: str, *, dialect: SamplingDialect
) -> datetime | None:
    try:
        result = run(freshness_sql(table, column, dialect=dialect))
    except SampleBudgetExceeded:
        return None
    if not result.rows:
        return None
    return _as_datetime(result.rows[0][0])


def _freshness_blocked(column: str, specs: list[ColumnSampleSpec]) -> bool:
    """MAX(column) is a literal too — never probe a shape-only column."""
    return any(c.shape_only for c in specs if c.name == column)


def _as_int(value: object) -> int | None:
    # Drivers disagree on aggregate types: int (most), float, Decimal (Snowflake).
    return int(value) if isinstance(value, int | float | Decimal) else None


def _as_datetime(value: object) -> datetime | None:
    """Best-effort UTC datetime from whatever MAX(time column) came back as."""
    if isinstance(value, datetime):  # before date — datetime is a date subclass
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None
