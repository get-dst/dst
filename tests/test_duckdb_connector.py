"""DuckDB connector contract tests against the jaffle fixture."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.protocols import Connector


@pytest.fixture
def conn() -> DuckDBConnector:
    return DuckDBConnector(settings.duckdb_jaffle_path)


def test_satisfies_protocol(conn: DuckDBConnector) -> None:
    assert isinstance(conn, Connector)


def test_introspect_jaffle(conn: DuckDBConnector) -> None:
    snap = conn.introspect()
    assert snap.dialect == "duckdb"
    by_name = {t.name: t for t in snap.tables}
    assert "customers" in by_name
    assert "orders" in by_name
    customers = by_name["customers"]
    assert customers.row_count == 100
    cols = {c.name for c in customers.columns}
    assert {"customer_id", "customer_lifetime_value", "number_of_orders"} <= cols
    # profiling populated
    clv = next(c for c in customers.columns if c.name == "customer_lifetime_value")
    assert clv.profile is not None and "distinct" in clv.profile


def test_execute_select(conn: DuckDBConnector) -> None:
    res = conn.execute("SELECT count(*) AS n FROM customers")
    assert res.columns == ["n"]
    assert res.rows == [[100]]


def test_execute_row_limit(conn: DuckDBConnector) -> None:
    res = conn.execute("SELECT customer_id FROM customers ORDER BY customer_id", row_limit=5)
    assert len(res.rows) == 5


def test_dry_run(conn: DuckDBConnector) -> None:
    assert conn.dry_run("SELECT 1").valid is True
    bad = conn.dry_run("SELECT * FROM does_not_exist")
    assert bad.valid is False and bad.error


def test_read_only_rejects_writes(conn: DuckDBConnector) -> None:
    with pytest.raises(duckdb.Error):
        conn.execute("CREATE TABLE evil (x int)")


# ── schemas other than `main` must not be invisible ──────────────────────────
# `SHOW TABLES` lists the CURRENT schema only, so a profile_catalog that
# hard-filters schema_name = 'main' returns a blank line and exit 0 for a valid
# warehouse whose tables live in another schema.


@pytest.fixture
def multi_schema(tmp_path: Path) -> str:
    path = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA spider")
    con.execute("CREATE TABLE spider.player (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO spider.player VALUES (1, 'a'), (2, 'b')")
    con.execute("CREATE VIEW spider.v_player AS SELECT id FROM spider.player")
    con.execute("CREATE SCHEMA marts")
    con.execute("CREATE TABLE marts.season (year INTEGER)")
    con.execute("CREATE TABLE main.top (x BIGINT)")
    con.close()
    return path


def test_introspect_sees_every_schema(multi_schema: str) -> None:
    snap = DuckDBConnector(multi_schema, profile=False).introspect()
    # qualified everywhere but `main`, which DuckDB resolves unqualified
    assert [t.name for t in snap.tables] == [
        "top",  # main, unqualified
        "marts.season",
        "spider.player",
        "spider.v_player",  # views count: jaffle's own stg_* models are views
    ]
    player = next(t for t in snap.tables if t.name == "spider.player")
    assert player.row_count == 2
    assert {c.name for c in player.columns} == {"id", "name"}
    assert snap.schemas_searched == ["main", "marts", "spider"]


def test_profile_catalog_sees_every_schema_and_every_view(multi_schema: str) -> None:
    """The catalog pass must name — and cover — exactly what introspect() lists.

    It read duckdb_tables() alone while introspect() unioned duckdb_views(), so a
    relation offered for authoring could never carry a profile fact, and every
    dbt staging model is a view."""
    conn = DuckDBConnector(multi_schema)
    profiles = conn.profile_catalog()
    assert [p.table for p in profiles] == [
        "top",
        "marts.season",
        "spider.player",
        "spider.v_player",
    ]
    assert {p.table for p in profiles} == {t.name for t in conn.introspect().tables}
    assert [c.name for c in profiles[2].columns] == ["id", "name"]


def test_schema_filter_scopes_both_passes(multi_schema: str) -> None:
    conn = DuckDBConnector(multi_schema, profile=False, schema="spider")
    assert [t.name for t in conn.introspect().tables] == ["spider.player", "spider.v_player"]
    assert [p.table for p in conn.profile_catalog()] == ["spider.player", "spider.v_player"]
    assert conn.introspect().schemas_searched == ["spider"]


def test_empty_warehouse_names_the_schemas_it_searched(tmp_path: Path) -> None:
    """No tables is a legitimate answer — a SILENT one never is."""
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    snap = DuckDBConnector(path).introspect()
    assert snap.tables == []
    assert snap.schemas_searched == ["main"]
