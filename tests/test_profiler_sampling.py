"""The guarded sampling pass. Connectors sample only what they're asked,
through dialect-correct capped SQL (USING SAMPLE / TABLESAMPLE SYSTEM / SAMPLE /
plain LIMIT); the orchestrator samples only the catalog pass's gaps, merges into
the stored profile, and upserts. The shape-only rule — an excluded column gets
null rate and cardinality, never literal values — is asserted at every layer.

DuckDB runs live on the jaffle fixture (and a scratch file with a shape-only
column); Postgres runs live against the docker test DB; BigQuery (including the
dry-run refusal path) and MySQL run against scripted clients that record every
SQL string issued."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services.config import settings
from services.connectors.bigquery import BigQueryConnector
from services.connectors.duckdb import DuckDBConnector
from services.connectors.mysql import MySQLConnector
from services.connectors.postgres import PostgresConnector
from services.connectors.sampling import (
    SampleBudgetExceeded,
    counts_exactly,
    freshness_sql,
    stats_sql,
    top_values_sql,
)
from services.connectors.tagging import MARKER
from services.contracts.fakes import FakeConnector
from services.contracts.profile import (
    EXACT_PROFILE_MAX_ROWS,
    LOW_CARDINALITY_MAX,
    ColumnProfile,
    ColumnSampleSpec,
    PartitioningProfile,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.protocols import Connector, SamplingProfiler
from services.db.session import org_session
from services.lenses import profile_store
from services.lenses.profiler import build_sample_spec, merge_sampled, run_sampling_pass
from services.lenses.profiler_catalog import run_catalog_pass


def _untag(sql: str) -> str:
    """Fakes here dispatch on statement prefixes; strip the dst tag first."""
    return sql.split("*/", 1)[1].lstrip() if sql.lstrip().startswith(MARKER) else sql


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


def _col(profile: TableProfile, name: str) -> ColumnProfile:
    return next(c for c in profile.columns if c.name == name)


# ---------------------------------------------------------------------------
# Dialect-correct, capped SQL — the pure builders, all five dialects
# ---------------------------------------------------------------------------

_SHAPE_COLS = [
    ColumnSampleSpec(name="status", type="VARCHAR"),
    ColumnSampleSpec(name="amount", type="DOUBLE"),
    ColumnSampleSpec(name="email", type="VARCHAR", shape_only=True),
]


def test_stats_sql_per_dialect() -> None:
    """The sampled form — what a table over `EXACT_PROFILE_MAX_ROWS` gets."""
    duck = stats_sql("orders", _SHAPE_COLS, dialect="duckdb", max_rows=50, row_count=1000)
    assert duck is not None
    assert "USING SAMPLE 50 ROWS" in duck
    assert 'approx_count_distinct("status")' in duck
    assert 'MIN("amount")' in duck and 'MAX("amount")' in duck  # numeric gets min/max
    assert 'MIN("status")' not in duck  # text does not
    assert 'MIN("email")' not in duck and 'MAX("email")' not in duck  # shape-only never
    assert 'COUNT("email")' in duck  # …but shape (nulls/cardinality) still collected

    pg = stats_sql("public.orders", _SHAPE_COLS, dialect="postgres", max_rows=50, row_count=1000)
    assert pg is not None
    assert "TABLESAMPLE SYSTEM (5.0)" in pg and "LIMIT 50" in pg  # 50 of 1000 rows = 5%
    assert 'COUNT(DISTINCT "status")' in pg and "APPROX" not in pg  # no approx aggregate
    assert '"public"."orders"' in pg

    bq = stats_sql("p.d.orders", _SHAPE_COLS, dialect="bigquery", max_rows=50, row_count=1000)
    assert bq is not None
    assert "TABLESAMPLE SYSTEM (5.0 PERCENT)" in bq and "LIMIT 50" in bq
    assert "APPROX_COUNT_DISTINCT(`status`)" in bq
    assert "`p.d.orders`" in bq  # one backtick pair around the dotted path

    sf = stats_sql(
        "DB.PUBLIC.ORDERS", _SHAPE_COLS, dialect="snowflake", max_rows=50, row_count=1000
    )
    assert sf is not None
    assert "SAMPLE (50 ROWS)" in sf
    assert 'APPROX_COUNT_DISTINCT("status")' in sf
    assert '"DB"."PUBLIC"."ORDERS"' in sf

    my = stats_sql("orders", _SHAPE_COLS, dialect="mysql", max_rows=50, row_count=1000)
    assert my is not None
    assert "LIMIT 50" in my and "`orders`" in my
    assert "TABLESAMPLE" not in my and "USING SAMPLE" not in my and " SAMPLE (" not in my
    assert "COUNT(DISTINCT `status`)" in my

    # unknown row count → percent-based dialects sample at 100, the LIMIT still caps
    unknown = stats_sql("t", _SHAPE_COLS, dialect="postgres", max_rows=50, row_count=None)
    assert unknown is not None and "TABLESAMPLE SYSTEM (100.0)" in unknown
    assert stats_sql("t", [], dialect="duckdb", max_rows=50, row_count=None) is None


def test_stats_sql_exact_reads_the_whole_table_with_count_distinct() -> None:
    """Exact mode drops the sample clause and the approximate aggregate in every
    dialect that has one — the aggregate returns one row either way, so the cap
    on rows *transferred* only ever bought inaccuracy."""
    for dialect in ("duckdb", "postgres", "bigquery", "snowflake", "mysql"):
        sql = stats_sql(
            "orders",
            _SHAPE_COLS,
            dialect=dialect,  # type: ignore[arg-type]
            max_rows=50,
            row_count=1000,
            exact=True,
        )
        assert sql is not None
        assert "SAMPLE" not in sql and "LIMIT" not in sql, dialect
        assert "APPROX" not in sql.upper(), dialect
        assert "COUNT(DISTINCT " in sql, dialect
        assert "_dst_sample" not in sql, dialect  # the table itself, not a derived one

    exact_top = top_values_sql(
        "orders", "status", dialect="duckdb", max_rows=50, row_count=None, limit=25, exact=True
    )
    assert "SAMPLE" not in exact_top and 'FROM "orders"' in exact_top


def test_counts_exactly_boundary() -> None:
    assert counts_exactly(EXACT_PROFILE_MAX_ROWS, EXACT_PROFILE_MAX_ROWS)  # at the threshold
    assert not counts_exactly(EXACT_PROFILE_MAX_ROWS + 1, EXACT_PROFILE_MAX_ROWS)
    # an unknown row count already read the whole relation — count it properly
    assert counts_exactly(None, EXACT_PROFILE_MAX_ROWS)


def test_top_values_and_freshness_sql() -> None:
    top = top_values_sql(
        "orders", "status", dialect="duckdb", max_rows=50, row_count=None, limit=25
    )
    assert "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 25" in top
    assert "IS NOT NULL" in top and "USING SAMPLE 50 ROWS" in top
    fresh = freshness_sql("p.d.t", "ordered_at", dialect="bigquery")
    assert fresh == "SELECT MAX(`ordered_at`) AS v FROM `p.d.t`"


# ---------------------------------------------------------------------------
# DuckDB — live against the jaffle fixture, with the issued SQL recorded
# ---------------------------------------------------------------------------


class _RecordingDuckDB:
    """Delegates to a real DuckDB connection, recording every SQL string."""

    def __init__(self, inner: Any, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._log.append(_untag(sql))
        return self._inner.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._inner.close()


def _record_duckdb(
    conn: DuckDBConnector, monkeypatch: pytest.MonkeyPatch, recorded: list[str]
) -> None:
    real_connect = conn._connect
    monkeypatch.setattr(conn, "_connect", lambda: _RecordingDuckDB(real_connect(), recorded))


def test_duckdb_sampling_on_jaffle(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = DuckDBConnector(settings.duckdb_jaffle_path)
    assert isinstance(conn, Connector) and isinstance(conn, SamplingProfiler)
    recorded: list[str] = []
    _record_duckdb(conn, monkeypatch, recorded)

    specs = [
        TableSampleSpec(
            table="orders",
            row_count=99,
            freshness_column="order_date",
            columns=[
                ColumnSampleSpec(name="status", type="VARCHAR"),
                ColumnSampleSpec(name="order_date", type="DATE"),
                ColumnSampleSpec(name="amount", type="DOUBLE"),
            ],
        ),
        TableSampleSpec(
            table="customers",
            columns=[ColumnSampleSpec(name="customer_name", type="VARCHAR")],
        ),
    ]
    by_table = {p.table: p for p in conn.sample_profile(specs)}
    orders = by_table["orders"]
    assert orders.source == "sampled"

    # a status-like low-cardinality column gets the real literals — all of them
    status = _col(orders, "status")
    assert status.null_rate == 0.0
    assert status.distinct_count == 3 and status.is_low_cardinality
    assert status.distinct_is_exact and status.values_complete
    assert status.top_values is not None
    assert set(status.top_values) == {"completed", "return_pending", "returned"}

    # a date column gets min/max; the freshness probe fills last_updated_logical
    order_date = _col(orders, "order_date")
    assert order_date.min == "2018-01-01" and order_date.max == "2018-03-27"
    assert order_date.top_values is None  # 68 distinct dates is not an enum
    assert orders.last_updated_logical == datetime(2018, 3, 27, tzinfo=UTC)

    amount = _col(orders, "amount")
    assert amount.min is not None and amount.max is not None

    # a name-classified column yields shape but never values
    customer_name = _col(by_table["customers"], "customer_name")
    assert customer_name.null_rate == 0.0
    assert customer_name.distinct_count is not None
    assert customer_name.distinct_count > LOW_CARDINALITY_MAX
    assert customer_name.top_values is None
    assert customer_name.min is None and customer_name.max is None

    # jaffle is far under the threshold, so nothing sampled: no sample clause at all
    assert orders.sampled_rows is None
    assert not any("USING SAMPLE" in q for q in recorded)
    assert any("COUNT(DISTINCT " in q for q in recorded)
    # …and no query ever pulled customer_name literals
    assert not any("customer_name" in q and ("GROUP BY" in q or "MIN(" in q) for q in recorded)

    # a table the catalog reports as over the threshold samples, and says so
    recorded.clear()
    big = specs[0].model_copy(update={"row_count": EXACT_PROFILE_MAX_ROWS + 1})
    [sampled] = conn.sample_profile([big], max_rows=10)
    assert any("USING SAMPLE 10 ROWS" in q for q in recorded)  # the custom cap lands too
    assert sampled.sampled_rows == 10
    sampled_status = _col(sampled, "status")
    assert not sampled_status.distinct_is_exact and not sampled_status.values_complete


def test_duckdb_shape_only_column_yields_shape_never_values(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = str(tmp_path / "shape.duckdb")
    scratch = duckdb.connect(db_path)
    scratch.execute(
        "CREATE TABLE signups AS SELECT * FROM (VALUES "
        "('a@mail.example', 'active'), ('b@mail.example', 'active'), ('c@mail.example', 'churned')"
        ") AS t(email, plan_status)"
    )
    scratch.close()

    conn = DuckDBConnector(db_path)
    recorded: list[str] = []
    _record_duckdb(conn, monkeypatch, recorded)
    spec = TableSampleSpec(
        table="signups",
        columns=[
            ColumnSampleSpec(name="email", type="VARCHAR", shape_only=True),
            ColumnSampleSpec(name="plan_status", type="VARCHAR"),
        ],
    )
    [profile] = conn.sample_profile([spec])

    email = _col(profile, "email")
    assert email.null_rate == 0.0 and email.distinct_count == 3  # shape collected
    assert email.is_low_cardinality  # cardinality alone would have qualified it…
    assert email.top_values is None and email.min is None and email.max is None  # …never values

    plan = _col(profile, "plan_status")
    assert plan.top_values == ["active", "churned"]  # an unexcluded enum still collects
    assert plan.values_complete  # counted over every row, so that is the whole dictionary

    assert not any("email" in q and "GROUP BY" in q for q in recorded)
    assert "a@mail.example" not in profile.model_dump_json()  # no literal escaped into the profile


# ---------------------------------------------------------------------------
# Postgres — live against the docker test DB: TABLESAMPLE SYSTEM, capped
# ---------------------------------------------------------------------------

PG_SCHEMA = "sampshop"


class _RecordingPG:
    """Delegates to a real psycopg connection, recording every SQL string."""

    def __init__(self, inner: Any, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append(_untag(sql))
        return self._inner.execute(sql, params)

    def close(self) -> None:
        self._inner.close()


def _pg_connector() -> PostgresConnector:
    url = make_url(settings.database_admin_url)
    return PostgresConnector(
        host=url.host or "localhost",
        port=url.port or 5432,
        database=url.database or "dst",
        user=url.username or "",
        password=url.password or "",
        schema=PG_SCHEMA,
    )


@needs_db
def test_postgres_sampling_tablesample_live(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.execute(
            text(
                f"CREATE TABLE {PG_SCHEMA}.events (id integer, status text, "
                f"contact_email text, amount numeric, created_at timestamptz)"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {PG_SCHEMA}.events "
                f"SELECT i, "
                f"CASE i % 3 WHEN 0 THEN 'new' WHEN 1 THEN 'paid' ELSE 'void' END, "
                f"'user' || i || '@example.com', i * 1.5, "
                f"TIMESTAMPTZ '2026-01-01 00:00:00+00' + (i || ' hours')::interval "
                f"FROM generate_series(1, 300) AS s(i)"
            )
        )
    try:
        conn = _pg_connector()
        assert isinstance(conn, SamplingProfiler)
        recorded: list[str] = []
        real_connect = conn._connect
        monkeypatch.setattr(
            conn, "_connect", lambda **kw: _RecordingPG(real_connect(**kw), recorded)
        )
        spec = TableSampleSpec(
            table=f"{PG_SCHEMA}.events",
            row_count=300,
            freshness_column="created_at",
            columns=[
                ColumnSampleSpec(name="status", type="text"),
                ColumnSampleSpec(name="contact_email", type="text"),  # classifier must catch
                ColumnSampleSpec(name="amount", type="numeric"),
                ColumnSampleSpec(name="created_at", type="timestamp with time zone"),
            ],
        )
        [events] = conn.sample_profile([spec])

        # 300 rows is under the threshold: no TABLESAMPLE, the whole table counted
        assert not any("TABLESAMPLE" in q for q in recorded)
        assert events.sampled_rows is None

        status = _col(events, "status")
        assert status.distinct_count == 3 and status.is_low_cardinality  # exact COUNT(DISTINCT)
        assert status.distinct_is_exact and status.values_complete
        assert status.top_values is not None
        assert set(status.top_values) == {"new", "paid", "void"}

        email = _col(events, "contact_email")
        assert email.distinct_count == 300  # shape collected
        assert email.top_values is None and email.min is None and email.max is None
        # no emitted SQL ever pulled its literals: no MIN/MAX over it, no GROUP BY on it
        assert not any('MIN("contact_email")' in q or 'MAX("contact_email")' in q for q in recorded)
        assert not any("contact_email" in q and "GROUP BY" in q for q in recorded)

        amount = _col(events, "amount")
        assert amount.min is not None and float(amount.min) == 1.5
        assert amount.max is not None and float(amount.max) == 450.0

        created = _col(events, "created_at")
        assert created.min is not None and created.max is not None
        assert events.last_updated_logical == datetime(2026, 1, 13, 12, 0, tzinfo=UTC)

        # over the threshold the TABLESAMPLE clause comes back — and really runs
        recorded.clear()
        big = spec.model_copy(update={"row_count": EXACT_PROFILE_MAX_ROWS + 1})
        [sampled] = conn.sample_profile([big])
        assert any("TABLESAMPLE SYSTEM (" in q and "LIMIT 10000" in q for q in recorded)
        assert sampled.sampled_rows is not None
        assert not _col(sampled, "status").distinct_is_exact
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))


# ---------------------------------------------------------------------------
# BigQuery — scripted client: TABLESAMPLE … PERCENT, dry-run bytes-gate first
# ---------------------------------------------------------------------------


class _FakeBQRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    @property
    def schema(self) -> list[Any]:
        return [SimpleNamespace(name=k) for k in (self._rows[0] if self._rows else {})]

    def __iter__(self) -> Any:
        return iter(self._rows)


_BQ_FRESH = datetime(2026, 6, 9, 23, 0, tzinfo=UTC)


class _FakeBQSamplingClient:
    """Answers dry runs with a fixed bytes estimate; scripts the sampling queries."""

    def __init__(self, dry_run_bytes: int) -> None:
        self._dry_run_bytes = dry_run_bytes
        self.dry_runs: list[str] = []
        self.executed: list[str] = []

    def query(self, sql: str, job_config: Any = None, timeout: float | None = None) -> Any:
        sql = _untag(sql)
        if job_config is not None and getattr(job_config, "dry_run", False):
            self.dry_runs.append(sql)
            return SimpleNamespace(total_bytes_processed=self._dry_run_bytes)
        self.executed.append(sql)
        return SimpleNamespace(
            result=lambda timeout=None: _FakeBQRows(self._respond(sql)), total_bytes_billed=7
        )

    def _respond(self, sql: str) -> list[dict[str, Any]]:
        if "AS n_rows" in sql:  # the stats query
            return [
                {
                    "n_rows": 10000,
                    "nn_0": 9900,
                    "dc_0": 3,
                    "nn_1": 10000,
                    "dc_1": 9000,
                    "mn_1": datetime(2024, 1, 1, tzinfo=UTC),
                    "mx_1": _BQ_FRESH,
                }
            ]
        if "GROUP BY" in sql:  # top values for the low-cardinality column
            return [
                {"v": "active", "n": 6000},
                {"v": "churned", "n": 3000},
                {"v": "trial", "n": 900},
            ]
        if sql.startswith("SELECT MAX("):  # the freshness probe
            return [{"v": _BQ_FRESH}]
        raise AssertionError(f"unexpected SQL executed: {sql}")


def _bq_connector(client: _FakeBQSamplingClient, budget: int = 2_000_000_000) -> BigQueryConnector:
    conn = BigQueryConnector.__new__(BigQueryConnector)  # skip credential loading
    conn._project = "proj"
    conn._dataset = "proj.jaffle"
    conn._max_bytes = 1_000_000_000
    conn._sample_bytes_budget = budget
    conn._client = client  # type: ignore[assignment]
    return conn


_BQ_SPEC = TableSampleSpec(
    table="proj.jaffle.events",
    row_count=100_000_000,  # over EXACT_PROFILE_MAX_ROWS → the sampled path
    freshness_column="ordered_at",
    columns=[
        ColumnSampleSpec(name="status", type="STRING"),
        ColumnSampleSpec(name="ordered_at", type="TIMESTAMP"),
    ],
)


def test_bigquery_sampling_sql_and_gate() -> None:
    client = _FakeBQSamplingClient(dry_run_bytes=50_000_000)  # well under budget
    conn = _bq_connector(client)
    assert isinstance(conn, SamplingProfiler)

    [events] = conn.sample_profile([_BQ_SPEC])

    # every executed query was dry-run first
    assert len(client.dry_runs) == len(client.executed) == 3
    stats = client.executed[0]
    assert "TABLESAMPLE SYSTEM (0.01 PERCENT)" in stats  # 10k of 100M rows
    assert "LIMIT 10000" in stats
    assert "APPROX_COUNT_DISTINCT(`status`)" in stats
    assert "`proj.jaffle.events`" in stats

    status = _col(events, "status")
    assert status.null_rate == 0.01 and status.distinct_count == 3
    assert status.top_values == ["active", "churned", "trial"]
    # sampled: the count is a lower bound and the dictionary may be missing members
    assert events.sampled_rows == 10000
    assert not status.distinct_is_exact and not status.values_complete
    ordered_at = _col(events, "ordered_at")
    assert ordered_at.min is not None and ordered_at.min.startswith("2024-01-01")
    assert events.last_updated_logical == _BQ_FRESH


def test_bigquery_small_table_counts_exactly_and_falls_back_on_budget() -> None:
    """Under the threshold BigQuery reads the whole table; if the bytes gate
    refuses that read, the pass degrades to a labelled sample rather than
    dropping the table (which is what it used to do)."""
    small = _BQ_SPEC.model_copy(update={"row_count": 1_000})
    client = _FakeBQSamplingClient(dry_run_bytes=50_000_000)
    [events] = _bq_connector(client).sample_profile([small])
    assert "TABLESAMPLE" not in client.executed[0]
    assert "COUNT(DISTINCT `status`)" in client.executed[0]
    assert events.sampled_rows is None and _col(events, "status").distinct_is_exact

    # now a runner that refuses only the unsampled read (it scans everything)
    gated = _FakeBQSamplingClient(dry_run_bytes=50_000_000)
    real_query = gated.query

    def refuse_full_scans(sql: str, job_config: Any = None, timeout: float | None = None) -> Any:
        if "TABLESAMPLE" not in _untag(sql) and "AS n_rows" in _untag(sql):
            return SimpleNamespace(total_bytes_processed=9_000_000_000)  # over budget
        return real_query(sql, job_config, timeout)

    gated.query = refuse_full_scans  # type: ignore[method-assign]
    [degraded] = _bq_connector(gated).sample_profile([small])
    assert any("TABLESAMPLE" in q for q in gated.executed)  # it sampled instead of giving up
    assert degraded.sampled_rows == 10000
    assert not _col(degraded, "status").distinct_is_exact


def test_bigquery_refuses_over_budget_tables() -> None:
    client = _FakeBQSamplingClient(dry_run_bytes=5_000_000_000)  # over the ~2 GB budget
    conn = _bq_connector(client)

    with pytest.raises(SampleBudgetExceeded):
        conn._sample_query("SELECT COUNT(*) AS n_rows FROM `proj.jaffle.events`")

    # the table is omitted (its catalog profile stands) and nothing was ever executed
    assert conn.sample_profile([_BQ_SPEC]) == []
    assert client.executed == []
    assert len(client.dry_runs) >= 1  # the gate ran; the scan did not


# ---------------------------------------------------------------------------
# MySQL — scripted cursor: plain LIMIT (no TABLESAMPLE), read-only session
# ---------------------------------------------------------------------------

_MY_FRESH = datetime(2026, 6, 8, 18, 30)


class _FakeMySQLSamplingCursor:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._rows: list[dict[str, Any]] = []
        self.description: list[tuple[str]] | None = None

    def __enter__(self) -> _FakeMySQLSamplingCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        sql = _untag(sql)
        self._log.append(sql)
        if sql.startswith("SET SESSION"):
            self._rows, self.description = [], None
            return
        if "AS n_rows" in sql:  # the stats query
            self._rows = [
                {
                    "n_rows": 800,
                    "nn_0": 792,
                    "dc_0": 4,
                    "nn_1": 800,
                    "dc_1": 5,  # low-cardinality — values would qualify, shape_only blocks
                    "nn_2": 780,
                    "dc_2": 250,
                    "mn_2": date(2026, 1, 2),
                    "mx_2": date(2026, 6, 8),
                }
            ]
        elif "GROUP BY" in sql:
            assert "`status`" in sql, f"top-values SQL for an unexpected column: {sql}"
            self._rows = [
                {"v": "paid", "n": 400},
                {"v": "open", "n": 250},
                {"v": "void", "n": 100},
                {"v": "draft", "n": 42},
            ]
        elif sql.startswith("SELECT MAX("):
            self._rows = [{"v": _MY_FRESH}]
        else:  # pragma: no cover — guarded by the asserts below
            raise AssertionError(sql)
        self.description = [(k,) for k in self._rows[0]]

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeMySQLSamplingCon:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def cursor(self) -> _FakeMySQLSamplingCursor:
        return _FakeMySQLSamplingCursor(self._log)

    def close(self) -> None:
        pass


def test_mysql_sampling_plain_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MySQLConnector(host="h", database="jaffle", user="u", password="p")
    assert isinstance(conn, SamplingProfiler)
    recorded: list[str] = []
    monkeypatch.setattr(conn, "_connect", lambda: _FakeMySQLSamplingCon(recorded))

    spec = TableSampleSpec(
        table="invoices",
        row_count=800,
        freshness_column="updated_at",  # a documented column can still drive freshness
        columns=[
            ColumnSampleSpec(name="status", type="varchar"),
            ColumnSampleSpec(name="email", type="varchar", shape_only=True),
            ColumnSampleSpec(name="issued_on", type="date"),
        ],
    )
    [invoices] = conn.sample_profile([spec])

    # 800 rows is under the threshold: the whole table, exact COUNT(DISTINCT …)
    stats = next(q for q in recorded if "AS n_rows" in q)
    assert "LIMIT" not in stats and "`invoices`" in stats
    assert "COUNT(DISTINCT `status`)" in stats and "APPROX" not in stats
    assert not any("TABLESAMPLE" in q or "USING SAMPLE" in q or " SAMPLE (" in q for q in recorded)
    assert any(q == "SET SESSION TRANSACTION READ ONLY" for q in recorded)  # read-only session

    status = _col(invoices, "status")
    assert status.null_rate == 0.01 and status.distinct_count == 4
    assert status.top_values == ["paid", "open", "void", "draft"]
    assert status.distinct_is_exact and status.values_complete

    # over the threshold MySQL's plain LIMIT is the sample, and the facts are labelled
    recorded.clear()
    big = spec.model_copy(update={"row_count": EXACT_PROFILE_MAX_ROWS + 1})
    [estimated] = conn.sample_profile([big])
    assert "LIMIT 10000" in next(q for q in recorded if "AS n_rows" in q)
    assert estimated.sampled_rows == 800
    assert not _col(estimated, "status").distinct_is_exact

    email = _col(invoices, "email")
    assert email.distinct_count == 5 and email.is_low_cardinality  # shape says enum-like…
    assert email.top_values is None and email.min is None and email.max is None  # …values never
    assert not any("`email`" in q and "GROUP BY" in q for q in recorded)

    issued = _col(invoices, "issued_on")
    assert issued.min == "2026-01-02" and issued.max == "2026-06-08"
    assert invoices.last_updated_logical == _MY_FRESH.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Orchestrator pieces — gaps, freshness pick, merge (pure)
# ---------------------------------------------------------------------------


def _stored_profile() -> TableProfile:
    return TableProfile(
        connection="shop",
        table="public.orders",
        row_count=1000,
        partitioning=PartitioningProfile(column="created_at", kind="range"),
        columns=[
            # pg_stats freebie → not a gap
            ColumnProfile(name="status", type="text", null_rate=0.01, top_values=["paid"]),
            # described in the warehouse → not a gap
            ColumnProfile(
                name="kind", type="text", description="order kind", description_source="warehouse"
            ),
            ColumnProfile(name="channel", type="text"),  # the gap
            ColumnProfile(name="internal_code", type="text"),  # the second gap
            ColumnProfile(name="created_at", type="timestamp without time zone"),  # gap, temporal
        ],
    )


def test_build_sample_spec_samples_only_gaps() -> None:
    spec = build_sample_spec(_stored_profile())
    assert spec is not None
    by_name = {c.name: c for c in spec.columns}
    # the catalog-profiled columns are skipped at SQL-build time, not post-hoc
    assert set(by_name) == {"channel", "internal_code", "created_at"}
    assert not any(c.shape_only for c in spec.columns)  # nothing excluded on this call
    assert spec.freshness_column == "created_at"  # the known partition column
    assert spec.row_count == 1000


def test_build_sample_spec_exclusions_and_nothing_to_do() -> None:
    """``exclude_columns`` is the one control over what the warehouse is read for:
    table-qualified or bare, it makes the column shape-only for the whole pass."""
    spec = build_sample_spec(_stored_profile(), frozenset({"public.orders.channel"}))
    assert spec is not None
    assert {c.name: c.shape_only for c in spec.columns}["channel"] is True
    bare = build_sample_spec(_stored_profile(), frozenset({"internal_code"}))
    assert bare is not None
    assert {c.name: c.shape_only for c in bare.columns}["internal_code"] is True

    documented = TableProfile(
        connection="shop",
        table="public.done",
        columns=[ColumnProfile(name="id", type="integer", description="pk", null_rate=0.0)],
    )
    assert build_sample_spec(documented) is None  # no gaps, no time column → skip the table


def test_freshness_column_preference() -> None:
    profile = _stored_profile().model_copy(update={"partitioning": None})
    profile.columns.append(ColumnProfile(name="updated_at", type="timestamptz"))
    spec = build_sample_spec(profile)
    assert spec is not None
    assert spec.freshness_column == "updated_at"  # an update stamp beats other time columns

    # an excluded time column is never probed (MAX(col) is a literal too)
    birthdays = TableProfile(
        connection="shop",
        table="public.people",
        columns=[ColumnProfile(name="birth_date", type="date")],
    )
    birthday_spec = build_sample_spec(birthdays, frozenset({"birth_date"}))
    assert birthday_spec is not None
    assert birthday_spec.freshness_column is None
    assert birthday_spec.columns[0].shape_only  # its shape may still be sampled


def test_merge_sampled_strips_literals_at_the_last_gate() -> None:
    base = TableProfile(
        connection="shop",
        table="signups",
        row_count=3,
        columns=[
            ColumnProfile(name="plan", type="text"),
            ColumnProfile(name="email", type="text"),
        ],
    )
    # a misbehaving connector returns literals for the excluded column anyway
    fragment = TableProfile(
        connection="whatever",
        table="signups",
        source="sampled",
        last_updated_logical=datetime(2026, 6, 1, tzinfo=UTC),
        columns=[
            ColumnProfile(
                name="plan",
                type="text",
                null_rate=0.0,
                distinct_count=2,
                is_low_cardinality=True,
                top_values=["pro", "free"],
            ),
            ColumnProfile(
                name="email",
                type="text",
                null_rate=0.0,
                distinct_count=3,
                is_low_cardinality=True,
                top_values=["a@mail.example"],
                min="a@mail.example",
                max="c@mail.example",
            ),
        ],
    )
    merged = merge_sampled(base, fragment, frozenset({"email"}))
    email = _col(merged, "email")
    assert email.null_rate == 0.0 and email.distinct_count == 3  # shape merged
    assert email.top_values is None and email.min is None and email.max is None  # values stripped
    assert _col(merged, "plan").top_values == ["pro", "free"]
    assert merged.last_updated_logical == datetime(2026, 6, 1, tzinfo=UTC)
    assert merged.source == "sampled"  # the catalog knew nothing about this table
    assert merged.row_count == 3  # catalog facts survive
    assert merged.connection == "shop"  # keyed to the registered connection, not the fragment


def test_merge_sampled_source_mixed_when_catalog_knew() -> None:
    base = _stored_profile()  # has descriptions + pg_stats columns
    fragment = TableProfile(
        connection="shop",
        table="public.orders",
        source="sampled",
        columns=[ColumnProfile(name="channel", type="text", null_rate=0.5, distinct_count=2)],
    )
    merged = merge_sampled(base, fragment)
    assert merged.source == "mixed"
    assert _col(merged, "channel").null_rate == 0.5
    assert _col(merged, "status").top_values == ["paid"]  # catalog stats untouched
    assert _col(merged, "kind").description == "order kind"
    # re-merging onto an established profile keeps its provenance
    assert merge_sampled(merged, fragment).source == "mixed"


# ---------------------------------------------------------------------------
# Orchestrator — end-to-end on jaffle + the store (gaps only, merge, persist)
# ---------------------------------------------------------------------------


def _org() -> str:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return str(
            c.execute(
                text("INSERT INTO org (name) VALUES ('SamplingPassTest') RETURNING id")
            ).scalar_one()
        )


def _cleanup(org: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM join_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM table_profile WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_run_sampling_pass_fills_gaps_merges_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = _org()
    try:
        conn = DuckDBConnector(settings.duckdb_jaffle_path)
        with org_session(org) as s:
            run_catalog_pass(s, conn, "jaffle")
        with org_session(org) as s:
            updated = run_sampling_pass(
                s,
                conn,
                "jaffle",
                tables=["orders", "customers"],
                exclude_columns=["orders.amount"],  # per-call exclusion → shape only
            )
        by_table = {p.table: p for p in updated}
        assert set(by_table) == {"orders", "customers"}

        orders = by_table["orders"]
        assert orders.source == "sampled"  # jaffle's catalog had no descriptions/stats to mix
        assert orders.row_count == 99  # catalog facts survive the merge
        status = _col(orders, "status")
        assert status.top_values is not None
        assert set(status.top_values) == {"completed", "return_pending", "returned"}
        assert status.null_rate == 0.0 and status.is_low_cardinality
        assert _col(orders, "order_date").min == "2018-01-01"
        assert orders.last_updated_logical == datetime(2018, 3, 27, tzinfo=UTC)

        amount = _col(orders, "amount")
        assert amount.null_rate is not None and amount.distinct_count is not None  # shape
        assert amount.top_values is None and amount.min is None and amount.max is None  # excluded

        customer_name = _col(by_table["customers"], "customer_name")
        assert customer_name.distinct_count is not None  # shape
        assert customer_name.top_values is None and customer_name.max is None  # classifier
        assert by_table["customers"].last_updated_logical is not None  # probed a date column

        # persisted: the store serves the merged profile back
        with org_session(org) as s:
            stored = profile_store.get_profile(s, "jaffle", "orders")
        assert stored is not None and stored.profile.source == "sampled"
        stored_status = _col(stored.profile, "status")
        assert stored_status.top_values is not None

        # second pass: every gap is now filled → only freshness probes run, no re-sampling
        recorded: list[str] = []
        _record_duckdb(conn, monkeypatch, recorded)
        with org_session(org) as s:
            again = run_sampling_pass(s, conn, "jaffle", tables=["orders", "customers"])
        assert recorded and all(q.startswith("SELECT MAX(") for q in recorded)
        orders_again = {p.table: p for p in again}["orders"]
        assert orders_again.source == "sampled"  # provenance sticks
        assert _col(orders_again, "status").top_values == status.top_values
        # the exclusion outcome is durable: amount is no longer a gap, so never re-sampled
        amount_again = _col(orders_again, "amount")
        assert amount_again.top_values is None and amount_again.min is None
    finally:
        _cleanup(org)


@needs_db
def test_sampling_pass_without_capability_is_a_no_op() -> None:
    assert not isinstance(FakeConnector(), SamplingProfiler)
    org = _org()
    try:
        profile = TableProfile(
            connection="plain",
            table="t",
            columns=[ColumnProfile(name="dark", type="text")],
        )
        with org_session(org) as s:
            profile_store.upsert_profile(s, profile)
        with org_session(org) as s:
            [unchanged] = run_sampling_pass(s, FakeConnector(), "plain")
        assert unchanged.source == "catalog"
        assert _col(unchanged, "dark").null_rate is None
    finally:
        _cleanup(org)


def test_views_sample_with_limit_not_tablesample() -> None:
    # TABLESAMPLE is a base-table-only construct on BigQuery/Postgres; one view
    # used to abort the whole connection's probe.
    for dialect in ("bigquery", "postgres"):
        sql = stats_sql(
            "p.d.dim_account",
            [ColumnSampleSpec(name="id", type="STRING")],
            dialect=dialect,  # type: ignore[arg-type]
            max_rows=500,
            row_count=1_000_000,
            is_view=True,
        )
        assert sql is not None and "TABLESAMPLE" not in sql and "LIMIT 500" in sql
        tabled = stats_sql(
            "p.d.orders",
            [ColumnSampleSpec(name="id", type="STRING")],
            dialect=dialect,  # type: ignore[arg-type]
            max_rows=500,
            row_count=1_000_000,
            is_view=False,
        )
        assert tabled is not None and "TABLESAMPLE" in tabled


def test_build_sample_spec_carries_is_view() -> None:
    profile = TableProfile(
        connection="c",
        table="p.d.v",
        is_view=True,
        columns=[ColumnProfile(name="x", type="STRING")],
    )
    spec = build_sample_spec(profile)
    assert spec is not None and spec.is_view is True


def test_one_failing_table_does_not_abort_the_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Per-table isolation: the failure is logged and the
    # healthy table still samples — the connection's probe survives.
    from services.connectors.sampling import sample_tables

    def run(sql: str) -> Any:
        if "broken_view" in sql:
            raise RuntimeError("sampling query failed dry run: TABLESAMPLE ... base")
        from services.contracts.warehouse import QueryResult

        return QueryResult(columns=["n_rows", "nn_0", "dc_0"], rows=[[3, 3, 2]])

    specs = [
        TableSampleSpec(table="d.broken_view", columns=[ColumnSampleSpec(name="a", type="INT")]),
        TableSampleSpec(table="d.healthy", columns=[ColumnSampleSpec(name="a", type="INT")]),
    ]
    with caplog.at_level("WARNING", logger="services.connectors.sampling"):
        profiles = sample_tables(
            run,
            specs,
            connection="c",
            dialect="bigquery",
            max_rows=500,
            max_distinct_for_enum=25,
        )
    assert [p.table for p in profiles] == ["d.healthy"]
    assert any("d.broken_view not sampled" in r.message for r in caplog.records)
