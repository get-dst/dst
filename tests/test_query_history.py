"""Query-history readers — contract, absence path, snowflake row mapping."""

from datetime import UTC, datetime
from pathlib import Path

from services.connectors.duckdb import DuckDBConnector
from services.connectors.snowflake import SnowflakeConnector
from services.contracts.query_history import HistoryReader, QueryRecord


def test_duckdb_absence_is_a_normal_answer(tmp_path: Path) -> None:
    import duckdb as _duck

    db = tmp_path / "t.duckdb"
    _duck.connect(str(db)).close()
    con = DuckDBConnector(path=str(db))
    assert isinstance(con, HistoryReader)
    assert con.query_history() == []


def test_snowflake_maps_history_rows(monkeypatch) -> None:
    rows = [
        (
            "SELECT SUM(revenue_eur) FROM gold.sales_revenue_monthly",
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 6, 10, tzinfo=UTC),
            14,
            "ANALYST_1",
            "Omni",
        )
    ]

    class _Cur:
        def execute(self, sql, params=None):
            assert "account_usage.query_history" in sql
            assert params["database"] == "NORTHWIND"
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Con:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    con = SnowflakeConnector(
        account="a", user="u", password="p", warehouse="w", database="NORTHWIND"
    )
    monkeypatch.setattr(con, "_connect", lambda: _Con())
    assert isinstance(con, HistoryReader)
    (rec,) = con.query_history(days=40)
    assert rec == QueryRecord(
        statement="SELECT SUM(revenue_eur) FROM gold.sales_revenue_monthly",
        first_seen=datetime(2026, 5, 1, tzinfo=UTC),
        last_seen=datetime(2026, 6, 10, tzinfo=UTC),
        run_count=14,
        principal="ANALYST_1",
        source_tool="Omni",
    )


def test_bigquery_maps_jobs_rows(monkeypatch) -> None:
    from services.connectors import bigquery as bq_mod

    rows = [
        {
            "query": "SELECT SUM(net_revenue_eur) FROM gold.fct_revenue_monthly",
            "first_seen": datetime(2026, 6, 1, tzinfo=UTC),
            "last_seen": datetime(2026, 6, 11, tzinfo=UTC),
            "run_count": 5,
            "principal": "analyst@northwind.example",
        }
    ]

    class _Job:
        def result(self, timeout=None):
            return rows

    class _Client:
        def get_dataset(self, ref, timeout=None):
            class _DS:
                location = "europe-north1"

            return _DS()

        def query(self, sql, job_config=None, timeout=None):
            assert "region-europe-north1" in sql and "JOBS_BY_PROJECT" in sql
            return _Job()

    con = bq_mod.BigQueryConnector.__new__(bq_mod.BigQueryConnector)
    con._project = "northwind-demo"
    con._dataset = "gold"
    con._client = _Client()
    assert isinstance(con, HistoryReader)
    (rec,) = con.query_history(days=30)
    assert rec.run_count == 5 and rec.principal == "analyst@northwind.example"
    assert rec.source_tool is None
