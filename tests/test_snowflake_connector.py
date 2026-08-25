"""Snowflake connector contract test (opt-in — hits real Snowflake).

Run with: DST_TEST_SNOWFLAKE=1 uv run pytest tests/test_snowflake_connector.py
Skipped by default so the suite stays fast + offline; the connector is also
exercised by the live draft->publish->query flow.
"""

from __future__ import annotations

import os

import pytest

from services.connectors.snowflake import SnowflakeConnector
from services.contracts.protocols import Connector

run_sf = pytest.mark.skipif(
    not os.environ.get("DST_TEST_SNOWFLAKE"),
    reason="set DST_TEST_SNOWFLAKE=1 + SNOWFLAKE_* env to run",
)


@run_sf
def test_snowflake_introspect_and_query() -> None:
    conn = SnowflakeConnector(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
    )
    assert isinstance(conn, Connector)

    snap = conn.introspect()
    assert snap.dialect == "snowflake"

    res = conn.execute("SELECT 1 AS n", row_limit=10)
    assert res.columns
    assert res.rows


@run_sf
@pytest.mark.skipif(
    not os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"),
    reason="keypair leg — set SNOWFLAKE_PRIVATE_KEY_PATH to a registered PKCS8 PEM",
)
def test_snowflake_keypair_auth_live() -> None:
    """Keypair against the real account — the auth method with a future,
    proven on the wire, not just at the kwarg-sniffing layer. Goes through
    from_record so the PEM-vs-password sniff is on the tested path too."""
    with open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]) as f:
        pem = f.read()
    conn = SnowflakeConnector.from_record(
        {
            "account": os.environ["SNOWFLAKE_ACCOUNT"],
            "user": os.environ["SNOWFLAKE_USER"],
            "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
            "database": os.environ["SNOWFLAKE_DATABASE"],
            "schema": os.environ.get("SNOWFLAKE_SCHEMA"),
            "role": os.environ.get("SNOWFLAKE_ROLE"),
        },
        pem,
    )
    assert conn._private_key is not None and conn._password is None

    res = conn.execute("SELECT current_user() AS u", row_limit=1)
    assert res.rows and res.rows[0][0] == os.environ["SNOWFLAKE_USER"]
