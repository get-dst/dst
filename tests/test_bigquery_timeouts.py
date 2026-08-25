"""Every BigQuery API call carries a timeout.

With no transport timeout, a read on a pooled keep-alive socket the remote has
already closed blocks forever: `dst apply` wedges the whole server and `dst
probe` sits indefinitely at 0% CPU with the profile unwritten. A hang must
become an error — so this pins the contract mechanically: a recording client
asserts that no call path reaches BigQuery without a bounded timeout."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.connectors.bigquery import BigQueryConnector


class _Recorder:
    """Records every client call's timeout kwarg; fails loudly on a bare call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def _note(self, name: str, timeout: float | None) -> None:
        self.calls.append((name, timeout))

    def list_datasets(self, project: str, timeout: float | None = None) -> list[SimpleNamespace]:
        self._note("list_datasets", timeout)
        return []

    def list_tables(self, dataset_ref: Any, timeout: float | None = None) -> list[SimpleNamespace]:
        self._note("list_tables", timeout)
        return []

    def get_table(self, reference: Any, timeout: float | None = None) -> SimpleNamespace:
        self._note("get_table", timeout)
        raise AssertionError("unreachable in these scans")

    def get_dataset(self, ref: Any, timeout: float | None = None) -> SimpleNamespace:
        self._note("get_dataset", timeout)
        return SimpleNamespace(location="EU")

    def query(
        self, sql: str, job_config: Any = None, timeout: float | None = None
    ) -> SimpleNamespace:
        self._note("query", timeout)

        class _Rows:
            schema: list[Any] = []

            def __iter__(self) -> Any:
                return iter([])

        def result(timeout: float | None = None) -> _Rows:
            self._note("job.result", timeout)
            return _Rows()

        return SimpleNamespace(result=result, total_bytes_billed=0, total_bytes_processed=0)


def _connector() -> tuple[Any, _Recorder]:
    conn = BigQueryConnector.__new__(BigQueryConnector)
    conn._project = "proj"
    conn._datasets = []
    conn._dataset = None
    conn._max_bytes = 10**9
    conn.max_bytes = 10**9
    conn._sample_bytes_budget = 10**9
    rec = _Recorder()
    conn._client = rec
    return conn, rec


def test_every_reached_call_carries_a_timeout() -> None:
    conn, rec = _connector()
    conn.introspect()
    conn.execute("SELECT 1", read_only=True)
    conn.dry_run("SELECT 1")
    conn.query_history(days=1, limit=1)
    conn.profile_catalog()
    bare = [(name, t) for name, t in rec.calls if t is None]
    assert rec.calls, "the recorder saw no calls — the harness is broken"
    assert not bare, f"BigQuery calls without a timeout: {bare}"


def test_job_waits_are_bounded_too() -> None:
    # The two-level contract: the API request has a read timeout AND the job
    # wait has a deadline — an accepted-but-never-answered job must also error.
    conn, rec = _connector()
    conn.execute("SELECT 1", read_only=True)
    waits = [t for name, t in rec.calls if name == "job.result"]
    assert waits and all(t is not None for t in waits)
