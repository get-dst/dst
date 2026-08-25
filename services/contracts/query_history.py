"""Query-history records — the wrongness probe's raw material.

A normalized view over each warehouse's own history catalog (Snowflake
ACCOUNT_USAGE.QUERY_HISTORY, BigQuery INFORMATION_SCHEMA.JOBS, Postgres
pg_stat_statements). Statements and metadata only — never row data. Connectors
that have no history catalog (DuckDB) return an empty list: absence is a
normal answer, not an error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class QueryRecord(BaseModel):
    """One distinct statement observed in the warehouse's history window."""

    statement: str
    first_seen: datetime
    last_seen: datetime
    run_count: int
    principal: str | None = None  # user/service account, when the catalog exposes it
    source_tool: str | None = None  # client application, when the catalog exposes it


@runtime_checkable
class HistoryReader(Protocol):
    """Connectors that can read their warehouse's query history."""

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        """Distinct SELECT statements seen in the window, most-run first."""
        ...
