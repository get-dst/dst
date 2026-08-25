"""Connection evaluation: read is always checked; write only when requested + supported."""

from __future__ import annotations

from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot, TableSchema
from services.lenses.connection_eval import evaluate_connection, normalize_access


class _ReadConnector:
    """A read-only connector (no probe_write) returning a canned schema."""

    kind = "fake"

    def __init__(self, *, tables: int = 2, read_error: str | None = None) -> None:
        self._tables = tables
        self._read_error = read_error

    def introspect(self) -> SchemaSnapshot:
        if self._read_error:
            raise RuntimeError(self._read_error)
        return SchemaSnapshot(
            connection="fake",
            dialect="duckdb",
            tables=[TableSchema(name=f"t{i}", columns=[]) for i in range(self._tables)],
        )

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        return QueryResult()


class _RWConnector(_ReadConnector):
    """A connector that also supports the write probe."""

    def __init__(self, *, write_error: str | None = None, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._write_error = write_error
        self.probed = False

    def probe_write(self) -> None:
        self.probed = True
        if self._write_error:
            raise RuntimeError(self._write_error)


def test_normalize_access_always_includes_read() -> None:
    assert normalize_access([]) == ["read"]
    assert normalize_access(["write"]) == ["read", "write"]
    assert normalize_access(["WRITE", "read", "bogus"]) == ["read", "write"]


def test_read_only_passes_without_write_probe() -> None:
    conn = _RWConnector()
    result = evaluate_connection(conn, ["read"])
    assert result.ok and result.tables == 2
    assert [c.stage for c in result.checks] == ["read"]
    assert conn.probed is False  # write not probed when not requested


def test_read_failure_blocks_and_skips_write() -> None:
    result = evaluate_connection(_RWConnector(read_error="auth denied"), ["read", "write"])
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.stage == "read" and "auth denied" in result.failure.error


def test_write_requested_and_passes() -> None:
    conn = _RWConnector()
    result = evaluate_connection(conn, ["read", "write"])
    assert result.ok and conn.probed is True
    assert [c.stage for c in result.checks] == ["read", "write"]


def test_write_requested_but_denied() -> None:
    result = evaluate_connection(_RWConnector(write_error="permission denied"), ["read", "write"])
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.stage == "write" and "permission denied" in result.failure.error


def test_write_requested_but_unsupported() -> None:
    # _ReadConnector has no probe_write -> not a WriteProbe.
    result = evaluate_connection(_ReadConnector(), ["read", "write"])
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.stage == "write" and "not supported" in result.failure.error
