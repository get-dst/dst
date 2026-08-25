"""MySQL connector contract test (opt-in — hits a real MySQL server).

Run with: DST_TEST_MYSQL=1 uv run pytest tests/test_mysql_connector.py
Skipped by default so the suite stays fast + offline; the connector is also
exercised by the live draft->publish->query flow.
"""

from __future__ import annotations

import os

import pytest

from services.connectors.mysql import MySQLConnector
from services.contracts.protocols import Connector

run_mysql = pytest.mark.skipif(
    not os.environ.get("DST_TEST_MYSQL"),
    reason="set DST_TEST_MYSQL=1 (+ MYSQL_* env) to run",
)


@run_mysql
def test_mysql_introspect_and_query() -> None:
    conn = MySQLConnector(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        database=os.environ["MYSQL_DATABASE"],
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
    )
    assert isinstance(conn, Connector)

    snap = conn.introspect()
    assert snap.dialect == "mysql"

    res = conn.execute("SELECT 1 AS n", row_limit=10)
    assert res.columns == ["n"]
    assert res.rows
