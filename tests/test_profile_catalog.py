"""The catalog pass. `profile_catalog()` per connector collects metadata
the warehouse already has (descriptions, partitions, row counts, last-updated,
pg_stats freebies, declared FKs) and provably issues no data-scanning SQL; the
orchestrator persists profiles + FK join candidates per connection.

DuckDB and Postgres run live (jaffle fixture / the docker test DB); BigQuery,
Snowflake and MySQL run against scripted stand-ins for their clients, which raise
on any SQL that isn't a catalog read."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services.config import settings
from services.connectors.bigquery import BigQueryConnector
from services.connectors.duckdb import DuckDBConnector
from services.connectors.mysql import MySQLConnector
from services.connectors.postgres import PostgresConnector
from services.connectors.snowflake import SnowflakeConnector, _parse_clustering_key
from services.contracts.fakes import FakeConnector
from services.contracts.profile import JoinCandidate, TableProfile, profiles_from_snapshot
from services.contracts.protocols import CatalogProfiler, Connector
from services.contracts.warehouse import ColumnSchema, SchemaSnapshot, TableSchema
from services.db.session import org_session
from services.lenses import profile_store
from services.lenses.profiler_catalog import run_catalog_pass


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


# ---------------------------------------------------------------------------
# DuckDB — live against the jaffle fixture, with the issued SQL recorded
# ---------------------------------------------------------------------------


class _RecordingDuckDB:
    """Delegates to a real DuckDB connection, recording every SQL string."""

    def __init__(self, inner: Any, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._log.append(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._inner.close()


def test_duckdb_profile_catalog_no_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = DuckDBConnector(settings.duckdb_jaffle_path)
    assert isinstance(conn, Connector) and isinstance(conn, CatalogProfiler)

    recorded: list[str] = []
    real_connect = conn._connect
    monkeypatch.setattr(conn, "_connect", lambda: _RecordingDuckDB(real_connect(), recorded))

    profiles = conn.profile_catalog()
    by_table = {p.table: p for p in profiles}
    assert {"customers", "orders"} <= set(by_table)

    customers = by_table["customers"]
    assert customers.row_count == 100  # the catalog's estimate matches the real count here
    assert customers.source == "catalog"
    cols = {c.name: c for c in customers.columns}
    assert cols["customer_id"].type == "INTEGER"

    # physical freshness == the database file's mtime
    import os

    mtime = datetime.fromtimestamp(os.stat(settings.duckdb_jaffle_path).st_mtime, tz=UTC)
    assert customers.last_updated_physical == mtime

    # every query hit catalog functions only — no table was scanned
    assert recorded, "expected the catalog queries to be recorded"
    assert all("duckdb_tables()" in q or "duckdb_columns()" in q for q in recorded)
    assert conn.catalog_join_candidates() == []


# ---------------------------------------------------------------------------
# Postgres — live against the docker test DB, pg_stats + comments + FKs
# ---------------------------------------------------------------------------

PG_SCHEMA = "profshop"


class _RecordingPG:
    """Delegates to a real psycopg connection, recording every SQL string."""

    def __init__(self, inner: Any, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append(sql)
        return self._inner.execute(sql, params)

    def close(self) -> None:
        self._inner.close()


def _pg_fixture(admin: Any) -> None:
    with admin.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.execute(text(f"CREATE TABLE {PG_SCHEMA}.customers (id integer PRIMARY KEY, name text)"))
        c.execute(
            text(
                f"CREATE TABLE {PG_SCHEMA}.orders ("
                f" id integer PRIMARY KEY,"
                f" customer_id integer REFERENCES {PG_SCHEMA}.customers(id),"
                f" status text)"
            )
        )
        c.execute(
            text(
                f"CREATE TABLE {PG_SCHEMA}.events (id integer, created_at date) "
                f"PARTITION BY RANGE (created_at)"
            )
        )
        c.execute(text(f"COMMENT ON TABLE {PG_SCHEMA}.orders IS 'jaffle orders'"))
        c.execute(text(f"COMMENT ON COLUMN {PG_SCHEMA}.orders.status IS 'order state'"))
        c.execute(
            text(
                f"INSERT INTO {PG_SCHEMA}.customers (id, name) "
                f"SELECT i, 'c' || i FROM generate_series(1, 50) AS s(i)"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {PG_SCHEMA}.orders (id, customer_id, status) "
                f"SELECT i, ((i - 1) % 50) + 1, "
                f"CASE WHEN i % 10 = 0 THEN NULL "
                f"     WHEN i % 3 = 0 THEN 'returned' "
                f"     WHEN i % 3 = 1 THEN 'completed' "
                f"     ELSE 'shipped' END "
                f"FROM generate_series(1, 100) AS s(i)"
            )
        )
        c.execute(text(f"ANALYZE {PG_SCHEMA}.customers"))
        c.execute(text(f"ANALYZE {PG_SCHEMA}.orders"))


@needs_db
def test_postgres_profile_catalog_pg_stats_fks_no_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = create_engine(settings.database_admin_url)
    _pg_fixture(admin)
    try:
        url = make_url(settings.database_admin_url)
        conn = PostgresConnector(
            host=url.host or "localhost",
            port=url.port or 5432,
            database=url.database or "dst",
            user=url.username or "",
            password=url.password or "",
            schema=PG_SCHEMA,
        )
        assert isinstance(conn, CatalogProfiler)

        recorded: list[str] = []
        real_connect = conn._connect
        monkeypatch.setattr(
            conn, "_connect", lambda **kw: _RecordingPG(real_connect(**kw), recorded)
        )

        profiles = conn.profile_catalog()
        candidates = conn.catalog_join_candidates()

        # no data-scanning SQL: every query reads pg_catalog/information_schema only,
        # and the profiled schema's name never appears inline (bind-parameter only)
        assert recorded
        for q in recorded:
            assert "pg_catalog." in q or "information_schema." in q, f"unexpected query: {q}"
            assert PG_SCHEMA not in q

        by_table = {p.table: p for p in profiles}
        orders = by_table[f"{PG_SCHEMA}.orders"]
        assert orders.description == "jaffle orders"
        assert orders.row_count == 100  # reltuples after ANALYZE
        assert orders.last_updated_physical is not None  # last_analyze

        cols = {c.name: c for c in orders.columns}
        status = cols["status"]
        assert status.description == "order state"
        assert status.description_source == "warehouse"
        assert status.null_rate is not None and 0.0 < status.null_rate < 0.2
        assert status.distinct_count == 3
        assert status.is_low_cardinality
        assert status.top_values is not None
        assert {"shipped", "completed", "returned"} >= set(status.top_values)
        assert "shipped" in status.top_values

        customer_id = cols["customer_id"]
        assert customer_id.distinct_count is not None and customer_id.distinct_count > 25
        assert not customer_id.is_low_cardinality
        assert customer_id.top_values is None  # literals only for low-cardinality columns

        events = by_table[f"{PG_SCHEMA}.events"]
        assert events.partitioning is not None
        assert events.partitioning.kind == "range"
        assert events.partitioning.column == "created_at"

        # declared FK → join candidate
        fk = next(c for c in candidates if c.left_table == f"{PG_SCHEMA}.orders")
        assert fk.evidence == "fk" and fk.status == "candidate"
        assert fk.left_columns == ["customer_id"]
        assert fk.right_table == f"{PG_SCHEMA}.customers" and fk.right_columns == ["id"]
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))


# ---------------------------------------------------------------------------
# BigQuery — scripted client; the only SQL allowed is INFORMATION_SCHEMA.PARTITIONS
# ---------------------------------------------------------------------------


class _FakeBQClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def list_tables(self, dataset_ref: Any, timeout: float | None = None) -> list[Any]:
        return [SimpleNamespace(reference="orders-ref")]

    def get_table(self, ref: Any, timeout: float | None = None) -> Any:
        assert ref == "orders-ref"
        return SimpleNamespace(
            project="proj",
            dataset_id="jaffle",
            table_id="orders",
            table_type="TABLE",
            description="jaffle orders",
            num_rows=99,
            time_partitioning=SimpleNamespace(field="ordered_at", type_="DAY"),
            range_partitioning=None,
            clustering_fields=["status"],
            modified=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            schema=[
                SimpleNamespace(name="id", field_type="INTEGER", mode="REQUIRED", description=None),
                SimpleNamespace(
                    name="status", field_type="STRING", mode="NULLABLE", description="order state"
                ),
                SimpleNamespace(
                    name="ordered_at", field_type="TIMESTAMP", mode="NULLABLE", description=None
                ),
            ],
        )

    def query(self, sql: str, job_config: Any = None, timeout: float | None = None) -> Any:
        assert "INFORMATION_SCHEMA.PARTITIONS" in sql, f"data-scanning SQL issued: {sql}"
        self.queries.append(sql)
        return SimpleNamespace(result=lambda timeout=None: [("orders", "20260601")])


def _bq_connector(client: _FakeBQClient) -> BigQueryConnector:
    conn = BigQueryConnector.__new__(BigQueryConnector)  # skip credential loading
    conn._project = "proj"
    conn._datasets = ["proj.jaffle"]
    conn._dataset = "proj.jaffle"
    conn._max_bytes = 1
    conn._client = client  # type: ignore[assignment]
    return conn


def test_bigquery_profile_catalog_metadata_only() -> None:
    client = _FakeBQClient()
    conn = _bq_connector(client)
    assert isinstance(conn, CatalogProfiler)

    [orders] = conn.profile_catalog()
    assert orders.table == "proj.jaffle.orders"
    assert orders.description == "jaffle orders"
    assert orders.row_count == 99
    assert orders.partitioning is not None
    assert orders.partitioning.column == "ordered_at" and orders.partitioning.kind == "time"
    assert orders.partitioning.latest_partition == "20260601"
    assert orders.clustering == ["status"]
    assert orders.last_updated_physical == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    status = next(c for c in orders.columns if c.name == "status")
    assert status.description == "order state" and status.description_source == "warehouse"
    assert not next(c for c in orders.columns if c.name == "id").nullable

    # exactly one query was issued, and it hit the PARTITIONS metadata view
    assert len(client.queries) == 1
    assert conn.catalog_join_candidates() == []


def test_bigquery_latest_partitions_degrades_on_error() -> None:
    class _Failing(_FakeBQClient):
        def query(self, sql: str, job_config: Any = None, timeout: float | None = None) -> Any:
            raise RuntimeError("PARTITIONS not visible in this region")

    [orders] = _bq_connector(_Failing()).profile_catalog()
    assert orders.partitioning is not None
    assert orders.partitioning.latest_partition is None  # best-effort, not fatal


# ---------------------------------------------------------------------------
# Snowflake — scripted cursor; only information_schema reads are answered
# ---------------------------------------------------------------------------

_SF_LAST_ALTERED = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)

# Two schemas on purpose: PUBLIC (the old hard-coded default) and MARTS.
_SF_ROWS: dict[str, list[dict[str, Any]]] = {
    "tables": [
        {
            "table_schema": "PUBLIC",
            "table_name": "ORDERS",
            "row_count": 99,
            "table_type": "BASE TABLE",
            "last_altered": _SF_LAST_ALTERED,
            "clustering_key": "LINEAR(STATUS, ORDERED_AT)",
            "comment": "orders",
        },
        {
            "table_schema": "MARTS",
            "table_name": "REVENUE",
            "row_count": 5,
            "table_type": "BASE TABLE",
            "last_altered": _SF_LAST_ALTERED,
            "clustering_key": None,
            "comment": None,
        },
    ],
    "columns": [
        {
            "table_schema": "MARTS",
            "table_name": "REVENUE",
            "column_name": "AMOUNT",
            "data_type": "NUMBER",
            "is_nullable": "YES",
            "comment": None,
        },
        {
            "table_schema": "PUBLIC",
            "table_name": "ORDERS",
            "column_name": "ID",
            "data_type": "NUMBER",
            "is_nullable": "NO",
            "comment": None,
        },
        {
            "table_schema": "PUBLIC",
            "table_name": "ORDERS",
            "column_name": "STATUS",
            "data_type": "TEXT",
            "is_nullable": "YES",
            "comment": "order state",
        },
    ],
    "schemata": [{"schema_name": "MARTS"}, {"schema_name": "PUBLIC"}],
}


class _FakeSnowflakeCursor:
    """Projects the query's own select list and honours its own scoping — so the
    fake can't pass a query that asks for columns or schemas it never returns."""

    def __init__(self) -> None:
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        low = " ".join(sql.lower().split())
        assert "information_schema." in low, f"data-scanning SQL issued: {sql}"
        view = next(v for v in _SF_ROWS if f"information_schema.{v}" in low)
        select = low.split("select ", 1)[1].split(" from ")[0]
        columns = [c.strip() for c in select.split(",")]
        rows = _SF_ROWS[view]
        if "= %s" in low:  # a declared schema pins exactly one
            key = "schema_name" if view == "schemata" else "table_schema"
            rows = [r for r in rows if r[key] == params[0]]
        self._rows = [tuple(r[c] for c in columns) for r in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        pass


class _FakeSnowflakeCon:
    def cursor(self) -> _FakeSnowflakeCursor:
        return _FakeSnowflakeCursor()

    def close(self) -> None:
        pass


def test_snowflake_profile_catalog_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = SnowflakeConnector(
        account="acc", user="u", password="p", warehouse="wh", database="DB", schema="PUBLIC"
    )
    assert isinstance(conn, CatalogProfiler)
    monkeypatch.setattr(conn, "_connect", lambda: _FakeSnowflakeCon())

    [orders] = conn.profile_catalog()
    assert orders.table == "DB.PUBLIC.ORDERS"
    assert orders.description == "orders"
    assert orders.row_count == 99
    assert orders.clustering == ["STATUS", "ORDERED_AT"]  # parsed from CLUSTERING_KEY
    assert orders.last_updated_physical == _SF_LAST_ALTERED
    status = next(c for c in orders.columns if c.name == "STATUS")
    assert status.nullable and status.description == "order state"
    assert conn.catalog_join_candidates() == []


def test_snowflake_without_a_declared_schema_spans_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same shape on Snowflake: schema defaulted to PUBLIC, so a warehouse
    whose tables live in MARTS introspected/profiled to nothing."""
    conn = SnowflakeConnector(account="acc", user="u", password="p", warehouse="wh", database="DB")
    monkeypatch.setattr(conn, "_connect", lambda: _FakeSnowflakeCon())

    assert [p.table for p in conn.profile_catalog()] == ["DB.PUBLIC.ORDERS", "DB.MARTS.REVENUE"]
    snapshot = conn.introspect()
    assert [t.name for t in snapshot.tables] == ["DB.MARTS.REVENUE", "DB.PUBLIC.ORDERS"]
    assert snapshot.schemas_searched == ["MARTS", "PUBLIC"]
    # a declared schema still scopes to exactly that one
    pinned = SnowflakeConnector(
        account="acc", user="u", password="p", warehouse="wh", database="DB", schema="MARTS"
    )
    monkeypatch.setattr(pinned, "_connect", lambda: _FakeSnowflakeCon())
    assert [t.name for t in pinned.introspect().tables] == ["DB.MARTS.REVENUE"]


def test_parse_clustering_key() -> None:
    assert _parse_clustering_key(None) == []
    assert _parse_clustering_key("") == []
    assert _parse_clustering_key("LINEAR(C1, C2)") == ["C1", "C2"]
    assert _parse_clustering_key("(C1)") == ["C1"]


# ---------------------------------------------------------------------------
# MySQL — scripted DictCursor; only information_schema reads are answered
# ---------------------------------------------------------------------------

_MY_UPDATE_TIME = datetime(2026, 6, 3, 7, 0)


class _FakeMySQLCursor:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeMySQLCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        low = sql.lower()
        assert "information_schema." in low, f"data-scanning SQL issued: {sql}"
        if "key_column_usage" in low:
            self._rows = [
                {
                    "constraint_name": "orders_ibfk_1",
                    "table_name": "orders",
                    "column_name": "customer_id",
                    "referenced_table_name": "customers",
                    "referenced_column_name": "id",
                }
            ]
        elif "information_schema.columns" in low:
            self._rows = [
                {
                    "table_name": "orders",
                    "column_name": "id",
                    "data_type": "int",
                    "is_nullable": "NO",
                    "column_comment": "",
                },
                {
                    "table_name": "orders",
                    "column_name": "status",
                    "data_type": "varchar",
                    "is_nullable": "YES",
                    "column_comment": "order state",
                },
            ]
        elif "information_schema.tables" in low:
            self._rows = [
                {
                    "table_name": "orders",
                    "table_rows": 99,
                    "update_time": _MY_UPDATE_TIME,
                    "table_comment": "jaffle orders",
                }
            ]
        else:  # pragma: no cover — guarded by the assert above
            raise AssertionError(sql)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeMySQLCon:
    def cursor(self) -> _FakeMySQLCursor:
        return _FakeMySQLCursor()

    def close(self) -> None:
        pass


def test_mysql_profile_catalog_and_fks(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MySQLConnector(host="h", database="jaffle", user="u", password="p")
    assert isinstance(conn, CatalogProfiler)
    monkeypatch.setattr(conn, "_connect", lambda: _FakeMySQLCon())

    [orders] = conn.profile_catalog()
    assert orders.table == "orders"  # bare name, exactly as introspect() names it
    assert orders.description == "jaffle orders"
    assert orders.row_count == 99
    assert orders.last_updated_physical == _MY_UPDATE_TIME
    status = next(c for c in orders.columns if c.name == "status")
    assert status.description == "order state" and status.description_source == "warehouse"
    assert next(c for c in orders.columns if c.name == "id").description is None

    [fk] = conn.catalog_join_candidates()
    assert fk.left_table == "orders" and fk.left_columns == ["customer_id"]
    assert fk.right_table == "customers" and fk.right_columns == ["id"]
    assert fk.evidence == "fk" and fk.status == "candidate"


# ---------------------------------------------------------------------------
# Orchestrator — persists profiles + candidates; falls back to introspect()
# ---------------------------------------------------------------------------


class _StubProfiler:
    """A `Connector` + `CatalogProfiler` whose execute() proves no scans happen."""

    kind = "duckdb"

    def __init__(self, tables: list[str] | None = None, row_count: int = 99) -> None:
        self._tables = tables if tables is not None else ["orders", "customers"]
        self._row_count = row_count

    def introspect(self) -> SchemaSnapshot:
        raise AssertionError("the catalog pass must not introspect a CatalogProfiler")

    def dry_run(self, sql: str) -> Any:
        raise AssertionError("the catalog pass must not dry-run")

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None) -> Any:
        raise AssertionError("the catalog pass must not execute SQL")

    def profile_catalog(self) -> list[TableProfile]:
        return [
            TableProfile(connection="its-own-label", table=t, row_count=self._row_count)
            for t in self._tables
        ]

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        return [
            JoinCandidate(
                left_table="orders",
                left_columns=["customer_id"],
                right_table="customers",
                right_columns=["id"],
                evidence="fk",
            )
        ]


def _org() -> object:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return c.execute(
            text("INSERT INTO org (name) VALUES ('CatalogPassTest') RETURNING id")
        ).scalar_one()


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM join_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM table_profile WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_run_catalog_pass_persists_profiles_and_candidates() -> None:
    org = _org()
    try:
        with org_session(org) as s:
            returned = run_catalog_pass(s, _StubProfiler(), "shop")
        assert all(p.connection == "shop" for p in returned)  # re-keyed to the registered name

        with org_session(org) as s:
            stored = profile_store.list_profiles(s, "shop")
            assert {p.profile.table for p in stored} == {"orders", "customers"}
            [cand] = profile_store.list_candidates(s, "shop")
            assert cand.evidence == "fk" and cand.status == "candidate"
            profile_store.approve_candidate(s, str(cand.id))

        # a second pass: upsert keeps the previous payload, candidates don't
        # duplicate or lose their status, dropped tables are pruned
        with org_session(org) as s:
            run_catalog_pass(s, _StubProfiler(tables=["orders"], row_count=123), "shop")
        with org_session(org) as s:
            stored_orders = profile_store.get_profile(s, "shop", "orders")
            assert stored_orders is not None
            assert stored_orders.profile.row_count == 123
            assert stored_orders.previous is not None
            assert stored_orders.previous.row_count == 99
            assert profile_store.get_profile(s, "shop", "customers") is None  # pruned
            [cand] = profile_store.list_candidates(s, "shop")
            assert cand.status == "approved"  # re-discovery never resets a decision
    finally:
        _cleanup(org)


@needs_db
def test_run_catalog_pass_falls_back_to_introspect() -> None:
    snapshot = SchemaSnapshot(
        connection="fake",
        dialect="duckdb",
        tables=[
            TableSchema(
                name="customers",
                columns=[
                    ColumnSchema(
                        name="id", type="INTEGER", nullable=False, profile={"distinct": 10}
                    )
                ],
                row_count=10,
            )
        ],
    )
    fake = FakeConnector(snapshot)
    assert not isinstance(fake, CatalogProfiler)  # plain connectors still profile

    org = _org()
    try:
        with org_session(org) as s:
            [profile] = run_catalog_pass(s, fake, "fallback")
        assert profile.connection == "fallback" and profile.source == "catalog"
        with org_session(org) as s:
            stored = profile_store.get_profile(s, "fallback", "customers")
            assert stored is not None
            assert stored.profile.row_count == 10
            [id_col] = stored.profile.columns
            assert id_col.distinct_count == 10 and id_col.is_low_cardinality
            assert profile_store.list_candidates(s, "fallback") == []
    finally:
        _cleanup(org)


def test_profiles_from_snapshot_defaults_to_snapshot_label() -> None:
    snapshot = SchemaSnapshot(
        connection="warehouse-label",
        dialect="duckdb",
        tables=[TableSchema(name="t", columns=[ColumnSchema(name="a", type="INTEGER")])],
    )
    [profile] = profiles_from_snapshot(snapshot)
    assert profile.connection == "warehouse-label"
    [overridden] = profiles_from_snapshot(snapshot, connection="registered-name")
    assert overridden.connection == "registered-name"
