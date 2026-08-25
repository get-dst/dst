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
