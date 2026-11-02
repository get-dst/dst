"""BigQuery connector contract test (opt-in — hits real BigQuery).

Run with: DST_TEST_BIGQUERY=1 uv run pytest tests/test_bigquery_connector.py
Skipped by default so the suite stays fast + offline; the connector is also
exercised by the live draft->publish->query flow.
"""

from __future__ import annotations

import os

import pytest

from services.config import settings
from services.connectors.bigquery import BigQueryConnector
from services.contracts.protocols import Connector

run_bq = pytest.mark.skipif(
    not (os.environ.get("DST_TEST_BIGQUERY") and settings.gcp_credentials),
    reason="set DST_TEST_BIGQUERY=1 + GCP_CREDENTIALS to run",
)


@run_bq
def test_bigquery_introspect_and_query() -> None:
    assert settings.gcp_credentials is not None
    conn = BigQueryConnector(
        settings.gcp_credentials,
        project=settings.gcp_project,
        dataset=settings.bigquery_dataset,
    )
    assert isinstance(conn, Connector)

    snap = conn.introspect()
    assert snap.dialect == "bigquery"
    assert any(t.name.endswith(".orders") for t in snap.tables)

    res = conn.execute(
        "SELECT status, COUNT(*) AS n "
        "FROM `bigquery-public-data.thelook_ecommerce.orders` GROUP BY status",
        row_limit=10,
    )
    assert res.columns == ["status", "n"]
    assert res.rows
    assert res.bytes_scanned is not None


@run_bq
def test_bigquery_sampling_accepts_every_column_type() -> None:
    """The sampling pass over a table of NUMERIC, STRING, STRUCT, JSON, ARRAY
    and GEOGRAPHY columns runs end to end: every statement is dry-run first by
    the connector, and BigQuery rejects DISTINCT aggregates over the
    non-groupable types unless they are keyed through TO_JSON_STRING.

    Builds a tiny table in a scratch dataset of the key's project and drops the
    dataset afterwards; nothing outside that table is scanned."""
    from google.cloud import bigquery

    from services.connectors.sampling import metered, sample_tables
    from services.contracts.profile import ColumnSampleSpec, TableSampleSpec

    assert settings.gcp_credentials is not None
    conn = BigQueryConnector(settings.gcp_credentials, project=settings.gcp_project)
    client = conn._client
    dataset = f"{conn._project}.dst_sampling_scratch"
    table = f"{dataset}.menagerie"
    client.create_dataset(bigquery.Dataset(dataset), exists_ok=True)
    try:
        client.query(
            f"CREATE OR REPLACE TABLE `{table}` AS "
            "SELECT 1 AS id, NUMERIC '1.5' AS price, 'a' AS label, "
            "STRUCT(1 AS x, 'y' AS y) AS nest, JSON '{\"k\": 1}' AS doc, "
            "['p', 'q'] AS tags, [NUMERIC '2'] AS amounts, ST_GEOGPOINT(1, 2) AS loc "
            "UNION ALL SELECT 2, NUMERIC '2.5', 'b', STRUCT(2, 'z'), JSON '{\"k\": 2}', "
            "['r'], [NUMERIC '3'], ST_GEOGPOINT(3, 4) "
            "UNION ALL SELECT 3, NUMERIC '2.5', 'b', STRUCT(2, 'z'), JSON '{\"k\": 2}', "
            "['r'], [NUMERIC '3'], ST_GEOGPOINT(3, 4)"
        ).result()
        conn = BigQueryConnector(
            settings.gcp_credentials, project=settings.gcp_project, dataset=dataset
        )
        snap = conn.introspect()
        schema = next(t for t in snap.tables if t.name == table)
        types = {c.name: c.type for c in schema.columns}
        assert types["price"] == "NUMERIC" and types["label"] == "STRING"
        assert types["nest"] == "RECORD" and types["doc"] == "JSON"
        assert types["tags"] == "ARRAY<STRING>" and types["amounts"] == "ARRAY<NUMERIC>"

        columns = [ColumnSampleSpec(name=c.name, type=c.type) for c in schema.columns]
        for exact in (True, False):  # the COUNT(DISTINCT …) read and the APPROX one
            # BigQuery refuses TABLESAMPLE on a table this small, so the sampled
            # read takes the LIMIT path a view takes.
            spec = TableSampleSpec(table=table, columns=columns, row_count=3, is_view=not exact)
            with metered() as meter:
                [profile] = sample_tables(
                    conn._sample_query,  # dry-run gate + budget cap, as sample_profile runs it
                    [spec],
                    connection=dataset,
                    dialect="bigquery",
                    max_rows=3,
                    max_distinct_for_enum=25,
                    exact_max_rows=3 if exact else 0,
                )
            print(f"exact={exact}: {meter.statements} statements, {meter.bytes_scanned} bytes")
            # BigQuery bills 10 MiB per statement at minimum; a scan beyond the
            # scratch table would show above it.
            assert meter.bytes_scanned <= meter.statements * 10 * 1024 * 1024
            assert profile.sampled_rows == (None if exact else 3)
            by_name = {c.name: c for c in profile.columns}
            assert by_name["price"].distinct_count == 2
            assert by_name["label"].distinct_count == 2
            assert by_name["nest"].distinct_count == 2
            assert by_name["doc"].distinct_count == 2
            assert by_name["tags"].distinct_count == 2
            assert by_name["amounts"].distinct_count == 2
            assert by_name["loc"].distinct_count == 2
            assert by_name["nest"].min is None and by_name["amounts"].min is None
            assert by_name["price"].min == "1.5"
    finally:
        client.delete_dataset(dataset, delete_contents=True, not_found_ok=True)
