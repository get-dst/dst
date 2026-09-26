"""A warehouse that never answers fails one served request loud, and only that one.

The failure: one question against a MotherDuck warehouse left the server busy for
hours. The connection open never returned; every later open of the same database
waited on it inside DuckDB's process-wide instance cache; the connection's
statement timeout never fired, because it starts once a connection exists; and
the serving path had no deadline of its own on the warehouse. Every later query
queued behind it.

Pinned here: a served request whose warehouse step stalls returns within the
deadline with a 504 naming the step and an error in the request log, the repair
loop does not re-ask a warehouse that is not answering, and the next request is
served, because no later open waits on the wedged one. A query that never returns
is interrupted at the deadline; one that does not stop gives up the held
connection, so the next request does not queue behind it.

The wedged open is real DuckDB: the connection string routes to a FIFO, whose
open() blocks until a writer appears, so DuckDB is stuck creating that cache
entry exactly as it is behind a stalled MotherDuck handshake.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.app import app
from services.config import settings
from services.connectors import duckdb as duckdb_connector
from services.connectors.duckdb import DuckDBConnector
from services.connectors.tagging import tag_sql
from services.contracts.fakes import FakeConnector, fake_llm_providers
from services.contracts.protocols import Connector
from services.contracts.warehouse import DryRunResult, QueryResult
from services.lenses.demo import jaffle_customer_value
from services.runtime.bounded import WarehouseTimeout
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)

SQL = "SELECT count(*) AS n FROM customers"


def _within(cap: float, fn: Callable[[], Any]) -> tuple[float, Any]:
    """Run *fn* on a thread and wait at most *cap* seconds for it: a regression
    fails this assertion instead of hanging the suite. Returns (seconds, result or
    the exception it raised)."""
    box: list[Any] = []
    started = time.perf_counter()

    def _run() -> None:
        try:
            box.append(fn())
        except BaseException as exc:  # noqa: BLE001 — handed back to the test
            box.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(cap)
    assert not worker.is_alive(), f"still waiting on the warehouse after {cap}s"
    return time.perf_counter() - started, box[0]


def _wedged_motherduck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[str], Callable[[], None]]:
    """An ``md:`` database whose cache entry never finishes opening.

    ``duckdb.connect`` stays real DuckDB: the plain connection string opens a FIFO
    (blocks in DuckDB's instance creation, and every other open of the same string
    waits on it), any other string opens a healthy file. Returns the path, the
    connection strings DuckDB was asked for, and a release that unblocks the
    wedged open so its thread ends with the test."""
    fifo = tmp_path / "wedged.duckdb"
    os.mkfifo(fifo)
    healthy = tmp_path / "healthy.duckdb"
    with duckdb.connect(str(healthy)) as con:
        con.execute("CREATE TABLE customers AS SELECT * FROM range(3) AS t(customer_id)")
    path = f"md:wedged_{uuid.uuid4().hex[:8]}"
    real_connect = duckdb.connect
    asked: list[str] = []

    def connect(database: str, read_only: bool = False, config: Any = None) -> Any:
        asked.append(database)
        return real_connect(str(fifo if database == path else healthy), read_only=True)

    monkeypatch.setattr(duckdb, "connect", connect)

    def release() -> None:
        # DuckDB opens the file more than once; hand each blocked open a writer
        # until nobody is waiting on the FIFO any more.
        misses = 0
        while misses < 10:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                misses = 0
            except OSError:
                misses += 1
            time.sleep(0.02)

    return path, asked, release


# ── the connector: a wedged open does not take later opens with it ───────────


def test_a_wedged_motherduck_open_fails_within_the_deadline_and_the_next_open_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, asked, release = _wedged_motherduck(tmp_path, monkeypatch)
    monkeypatch.setattr(duckdb_connector, "OPEN_TIMEOUT_S", 0.5)
    conn = DuckDBConnector(path, token="t")
    try:
        # A call arriving while the first open is wedged waits behind it, bounded
        # and outside DuckDB — then gets a fresh instance, not the wedged one.
        queued: list[tuple[float, Any]] = []
        behind = threading.Thread(
            target=lambda: (time.sleep(0.1), queued.append(_within(5, lambda: conn.execute(SQL)))),
            daemon=True,
        )
        behind.start()
        seconds, first = _within(5, lambda: conn.execute(SQL))
        behind.join(6)

        assert isinstance(first, WarehouseTimeout), first
        assert f"opening MotherDuck database '{path}'" in str(first)
        assert seconds < 2.5
        (queued_seconds, queued_outcome), *_ = queued
        assert isinstance(queued_outcome, QueryResult), queued_outcome
        assert queued_outcome.rows == [[3]]
        assert queued_seconds < 2.5
        # One open entered DuckDB for the wedged string, one for its successor.
        assert asked == [path, f"{path}?user=dst-1"]

        # Later calls are served at once on the held connection: no new open.
        seconds, served = _within(5, lambda: conn.execute(SQL))
        assert isinstance(served, QueryResult), served
        assert served.rows == [[3]]
        assert seconds < 1.0
        assert len(asked) == 2
    finally:
        release()


# ── the pipeline: a stalled step ends the request, never the repair loop ─────


class _Stalling(FakeConnector):
    """A warehouse that accepts the step and never answers it. The wait is
    capped so a regression fails the test instead of hanging it."""

    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage
        self.calls: dict[str, int] = {"dry_run": 0, "execute": 0}
        self.tagged: list[str] = []
        self.release = threading.Event()

    def dry_run(self, sql: str) -> DryRunResult:
        self.calls["dry_run"] += 1
        if self.stage == "dry_run":
            self.release.wait(10)
        return super().dry_run(sql)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        self.calls["execute"] += 1
        self.tagged.append(tag_sql(sql))  # what a real connector would send
        if self.stage == "execute":
            self.release.wait(10)
        return super().execute(sql, read_only=read_only, row_limit=row_limit)


def _serve(connector: Connector) -> Any:
    return run_query(
        question="How many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=connector,
        generator=FixedSQLGenerator(SQL),
        composer=None,
        max_repairs=2,
    )


@pytest.mark.parametrize(("stage", "step"), [("dry_run", "the dry run"), ("execute", "the query")])
def test_a_stalled_warehouse_step_fails_the_serve_at_the_deadline_without_repairs(
    stage: str, step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "serving_timeout_s", 0.5)
    stalling = _Stalling(stage)
    try:
        seconds, res = _within(5, lambda: _serve(stalling))
    finally:
        stalling.release.set()
    assert res.response.status == "error"
    assert res.trace.status == "error"
    assert res.error_kind == "timeout"
    assert step in (res.trace.error or "")
    assert "DST_SERVING_TIMEOUT_S" in (res.trace.error or "")
    assert seconds < 2.5
    # A warehouse that is not answering is not asked again by the repair loop.
    assert stalling.calls[stage] == 1
    if stage == "dry_run":
        assert stalling.calls["execute"] == 0


def test_a_bounded_query_keeps_its_serving_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound runs the step on its own thread; the query must still carry the
    request's tag (the context travels with it)."""
    monkeypatch.setattr(settings, "serving_timeout_s", 5)
    healthy = _Stalling("none")
    res = _serve(healthy)
    assert res.response.status == "ok"
    assert healthy.tagged and '"purpose": "serve"' in healthy.tagged[0]


# ── a stuck query: interrupted, and the held connection let go if it stays ───


class _StuckStatement:
    """A held MotherDuck connection over a healthy file whose first query never
    returns. Its statements take turns, so a later cursor's statement queues behind
    the stuck one. ``heeds_interrupt``: whether interrupting the stuck statement
    ends it."""

    def __init__(self, real: duckdb.DuckDBPyConnection, heeds_interrupt: bool) -> None:
        self.real = real
        self.heeds_interrupt = heeds_interrupt
        self.turn = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.interrupts = 0

    def cursor(self) -> _StuckCursor:
        return _StuckCursor(self, self.real.cursor())


class _StuckCursor:
    def __init__(self, conn: _StuckStatement, real: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.real = real

    def execute(self, query: str) -> Any:
        with self.conn.turn:
            if not query.startswith("EXPLAIN") and not self.conn.started.is_set():
                self.conn.started.set()
                self.conn.release.wait(10)
                if self.conn.heeds_interrupt:
                    raise duckdb.InterruptException("INTERRUPT Error: Interrupted!")
            return self.real.execute(query)

    def interrupt(self) -> None:
        self.conn.interrupts += 1
        if self.conn.heeds_interrupt:
            self.conn.release.set()

    def close(self) -> None:
        self.real.close()


def _stuck_motherduck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, heeds_interrupt: bool
) -> tuple[DuckDBConnector, _StuckStatement, list[str]]:
    healthy = tmp_path / "healthy.duckdb"
    with duckdb.connect(str(healthy)) as con:
        con.execute("CREATE TABLE customers AS SELECT * FROM range(3) AS t(customer_id)")
    path = f"md:stuck_{uuid.uuid4().hex[:8]}"
    real_connect = duckdb.connect
    stuck = _StuckStatement(real_connect(str(healthy), read_only=True), heeds_interrupt)
    opened: list[str] = []

    def connect(database: str, read_only: bool = False, config: Any = None) -> Any:
        opened.append(database)
        return stuck if database == path else real_connect(str(healthy), read_only=True)

    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(settings, "serving_timeout_s", 1.0)
    monkeypatch.setattr(duckdb_connector, "INTERRUPT_GRACE_S", 0.2)
    return DuckDBConnector(path, token="t"), stuck, opened


def test_a_query_past_the_deadline_that_ignores_its_interrupt_retires_the_held_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, stuck, opened = _stuck_motherduck(tmp_path, monkeypatch, heeds_interrupt=False)
    try:
        seconds, first = _within(5, lambda: _serve(conn))
        assert first.error_kind == "timeout"
        assert "the query" in (first.trace.error or "")
        assert seconds < 2.5
        assert stuck.interrupts == 1

        # The stuck statement still holds its connection: the next request runs on
        # a fresh instance, at its normal latency, instead of queueing behind it.
        seconds, second = _within(5, lambda: _serve(conn))
        assert second.response.status == "ok", second.trace.error
        assert second.response.data.rows == [[3]]
        assert seconds < 0.5
        assert opened == [conn._path, f"{conn._path}?user=dst-1"]
    finally:
        stuck.release.set()


def test_a_query_past_the_deadline_is_interrupted_and_the_connection_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, stuck, opened = _stuck_motherduck(tmp_path, monkeypatch, heeds_interrupt=True)
    try:
        seconds, first = _within(5, lambda: _serve(conn))
        assert first.error_kind == "timeout"
        assert seconds < 2.5
        assert stuck.interrupts == 1

        # Interrupted, the statement let go: the held connection serves the next
        # request at its normal latency, and nothing reopens.
        seconds, second = _within(5, lambda: _serve(conn))
        assert second.response.status == "ok", second.trace.error
        assert second.response.data.rows == [[3]]
        assert seconds < 0.5
        assert opened == [conn._path]
    finally:
        stuck.release.set()


# ── the door: a 504 naming the step, then the next request is served ─────────


@needs_db
def test_the_query_door_answers_504_naming_the_step_and_serves_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _asked, release = _wedged_motherduck(tmp_path, monkeypatch)
    monkeypatch.setattr(duckdb_connector, "OPEN_TIMEOUT_S", 0.5)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        query_api, "resolve_connector", lambda *a, **k: DuckDBConnector(path, token="t")
    )
    monkeypatch.setattr(
        query_api.assembly,
        "select_generators",
        lambda *a, **k: (FixedSQLGenerator(SQL), None, "none", None),
    )
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)

    def ask() -> Any:
        return client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?", "format": "structured"},
            headers={"Authorization": f"Bearer {raw}"},
        )

    try:
        seconds, first = _within(10, ask)
        assert first.status_code == 504, first.text
        assert f"opening MotherDuck database '{path}'" in first.json()["detail"]
        assert seconds < 5

        admin = create_engine(settings.database_admin_url)
        with admin.connect() as c:
            logged = c.execute(
                text("SELECT status, error FROM request_log WHERE org_id = :o"), {"o": org}
            ).all()
        assert [row[0] for row in logged] == ["error"]
        assert "opening MotherDuck database" in (logged[0][1] or "")

        seconds, second = _within(10, ask)
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "ok"
        assert second.json()["data"]["rows"] == [[3]]
    finally:
        release()
        _cleanup(settings.database_admin_url, org)
