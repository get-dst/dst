"""DuckDB connector: the statement timeout and the MotherDuck seam.

Both exist for a warehouse a stranger can reach — a public demo over MotherDuck
was the first. The timeout is pinned locally (an interrupt is an interrupt
whichever end of the connection is remote); the MotherDuck open is pinned
without a network round-trip, because a token is not a test fixture.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector, is_motherduck
from services.lenses.connections import build_connector

# --- statement timeout -------------------------------------------------------


def test_statement_timeout_cancels_and_names_the_cap() -> None:
    bounded = DuckDBConnector(settings.duckdb_jaffle_path, statement_timeout_ms=50)
    # A cross join over the biggest fixture table: seconds of work, cancelled in ms.
    slow = (
        "SELECT count(*) FROM web_sessions a, web_sessions b, web_sessions c "
        "WHERE a.session_id <> b.session_id"
    )
    with pytest.raises(duckdb.Error, match="statement_timeout_ms"):
        bounded.execute(slow)
    # The fast path is untouched — and the timer is cancelled, not left to fire later.
    assert bounded.execute("SELECT 1 AS one").rows == [[1]]


def test_no_timeout_by_default() -> None:
    assert DuckDBConnector(settings.duckdb_jaffle_path).statement_timeout_ms is None
    assert (
        DuckDBConnector(settings.duckdb_jaffle_path, statement_timeout_ms=0).statement_timeout_ms
        is None
    )


def test_factory_reads_the_timeout_key() -> None:
    conn = build_connector(
        "duckdb", {"path": settings.duckdb_jaffle_path, "statement_timeout_ms": 250}, None
    )
    assert isinstance(conn, DuckDBConnector)
    assert conn.statement_timeout_ms == 250


# --- MotherDuck --------------------------------------------------------------


def test_md_prefix() -> None:
    assert is_motherduck("md:dst_demo")
    assert not is_motherduck("fixtures/jaffle_shop.duckdb")


def test_token_in_path_is_refused() -> None:
    with pytest.raises(ValueError, match="secret_env"):
        DuckDBConnector("md:demo?motherduck_token=abc")


def test_local_file_is_read_only_whatever_the_flag() -> None:
    c = DuckDBConnector(settings.duckdb_jaffle_path, read_only=False)
    with pytest.raises(duckdb.Error):
        c.execute("CREATE TABLE should_not_exist (x INTEGER)")


def test_motherduck_connect_carries_token_and_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The md: path opens with the token in config (never in the path) and the
    declared access mode — pinned without a network round-trip."""
    seen: list[tuple[str, bool, dict[str, str]]] = []

    def fake_connect(
        path: str, read_only: bool = False, config: dict[str, str] | None = None
    ) -> None:
        seen.append((path, read_only, config or {}))
        raise duckdb.Error("offline")

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    with pytest.raises(duckdb.Error):
        DuckDBConnector("md:dst_demo", token="mdtok").execute("SELECT 1")
    with pytest.raises(duckdb.Error):
        build_connector("duckdb", {"path": "md:dst_demo", "read_only": False}, "mdtok").execute(
            "SELECT 1"
        )
    assert seen == [
        ("md:dst_demo", True, {"motherduck_token": "mdtok"}),
        ("md:dst_demo", False, {"motherduck_token": "mdtok"}),
    ]


def test_catalog_is_pinned_to_this_database(tmp_path: Path) -> None:
    """An attached database (every MotherDuck database in the account, or a local
    ATTACH) is not this connection's warehouse: nothing of it is introspected."""
    mine = tmp_path / "mine.duckdb"
    other = tmp_path / "other.duckdb"
    with duckdb.connect(str(other)) as c:
        c.execute("CREATE SCHEMA marts")
        c.execute("CREATE TABLE marts.leaked (x INTEGER)")
    with duckdb.connect(str(mine)) as c:
        c.execute("CREATE TABLE customers (id INTEGER)")

    class Attaching(DuckDBConnector):
        def _connect(self) -> duckdb.DuckDBPyConnection:
            con = super()._connect()
            con.execute(f"ATTACH '{other}' AS other (READ_ONLY)")
            return con

    conn = Attaching(str(mine))
    snap = conn.introspect()
    assert [t.name for t in snap.tables] == ["customers"]
    assert snap.schemas_searched == ["main"]
    assert [p.table for p in conn.profile_catalog()] == ["customers"]
