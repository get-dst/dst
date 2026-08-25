"""Connection evaluation — prove a warehouse credential can connect, read, and write.

Run at connection-create time (and on demand from the Test button): connectivity +
read are proven by introspecting the schema; write (when requested) is proven by a
`WriteProbe` that creates and drops a throwaway table. Read is always required; write is
only checked when the connection requests it. The first failing stage short-circuits.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from services.contracts.protocols import Connector, WriteProbe


@dataclass
class AccessCheck:
    stage: str  # "read" | "write"
    ok: bool
    error: str | None = None


@dataclass
class EvalResult:
    ok: bool
    checks: list[AccessCheck] = field(default_factory=list)
    tables: int | None = None  # table count from the read probe (introspection)

    @property
    def failure(self) -> AccessCheck | None:
        return next((c for c in self.checks if not c.ok), None)


def normalize_access(access: Iterable[str]) -> list[str]:
    """Read is always implied; keep a stable [read, write] order, drop unknowns."""
    requested = {a.strip().lower() for a in access}
    return [a for a in ("read", "write") if a == "read" or a in requested]


def evaluate_connection(connector: Connector, access: Iterable[str]) -> EvalResult:
    wanted = set(normalize_access(access))
    result = EvalResult(ok=False)

    # Read probe: introspection exercises connectivity + auth + SELECT on the schema.
    try:
        snapshot = connector.introspect()
        result.tables = len(snapshot.tables)
        result.checks.append(AccessCheck("read", True))
    except Exception as exc:  # noqa: BLE001 — any driver/credential error means no read
        result.checks.append(AccessCheck("read", False, str(exc)))
        return result

    if "write" in wanted:
        if not isinstance(connector, WriteProbe):
            result.checks.append(
                AccessCheck("write", False, "write verification is not supported for this type")
            )
            return result
        try:
            connector.probe_write()
            result.checks.append(AccessCheck("write", True))
        except Exception as exc:  # noqa: BLE001 — surface the exact write failure
            result.checks.append(AccessCheck("write", False, str(exc)))
            return result

    result.ok = True
    return result


_HISTORY_GRANT_HINTS = {
    "bigquery": "grant BigQuery Resource Viewer at project level",
    "snowflake": "grant IMPORTED PRIVILEGES on the SNOWFLAKE database",
}


def capability_report(connector: Connector) -> str:
    """One line naming what this credential can do beyond the read probe.

    Computed when a connection is (re)applied — a permission gap must be a
    visible degradation at deploy time, not a silent nightly skip in a server
    log nobody reads — the drift audit's own warning lands only there. Cheap
    and never fatal: a check that cannot run reports itself as the gap."""
    from services.contracts.query_history import HistoryReader

    parts: list[str] = []
    try:
        dry = connector.dry_run("SELECT 1")
        parts.append("query ✓" if dry.valid else f"query ✗ ({dry.error})")
    except Exception as exc:  # noqa: BLE001 — the report IS the failure channel
        parts.append(f"query ✗ ({str(exc).splitlines()[0][:120]})")
    if not isinstance(connector, HistoryReader):
        parts.append("query history — unavailable for this connection type (drift audits off)")
    else:
        try:
            connector.query_history(days=1, limit=1)
            parts.append("query history ✓")
        except Exception as exc:  # noqa: BLE001 — a permission gap, reported not raised
            hint = _HISTORY_GRANT_HINTS.get(getattr(connector, "kind", ""), "")
            tail = f" — {hint}" if hint else ""
            first = str(exc).splitlines()[0][:120]
            parts.append(f"query history ✗ (drift audits disabled: {first}{tail})")
    return " · ".join(parts)
