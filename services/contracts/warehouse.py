"""Warehouse-facing schemas: introspection snapshot + query results.

Shared by all `Connector` implementations (DuckDB, BigQuery) and consumed by the
semantic-model drafter, schema snapshots, and the runtime.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# dst's reserved namespace inside the customer's warehouse. The eval snapshot
# plane that lived here was removed — certified answers are the sole eval
# mechanism — but the name stays reserved: hard-excluded from every
# lens's sql_guard allowlist and hidden from introspection, so a leftover
# __dst schema from an older install can never be read as data.
VALIDATION_SCHEMA = "__dst"


class ColumnSchema(BaseModel):
    name: str
    type: str
    nullable: bool = True
    description: str | None = None
    profile: dict[str, object] | None = None  # value profiling (min/max/distinct/etc.)


class TableSchema(BaseModel):
    name: str  # fully-qualified, e.g. "analytics-prod.mart.mart_accounts"
    columns: list[ColumnSchema]
    row_count: int | None = None
    # None = the connector's catalog did not distinguish. Views were listed for
    # authoring with nothing saying they were views, so an author could not tell
    # a materialised fact from a derived one.
    is_view: bool | None = None
    partitioning: str | None = None
    clustering: list[str] = Field(default_factory=list)
    description: str | None = None


class SchemaSnapshot(BaseModel):
    connection: str
    dialect: str
    tables: list[TableSchema] = Field(default_factory=list)
    # What the connector actually looked in. An empty `tables` is never allowed to
    # read as "the warehouse is empty" — a blank line and exit 0 from a schema the
    # connector never searched is indistinguishable from a real empty warehouse, so
    # every empty-result message names these.
    schemas_searched: list[str] = Field(default_factory=list)
    # The listing hit the connector's cap before the scan finished. A capped
    # listing that reads as the whole warehouse poisons every downstream match,
    # so renderers must say so.
    truncated: bool = False


class QueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    bytes_scanned: int | None = None


class DryRunResult(BaseModel):
    bytes_estimated: int | None = None
    valid: bool = True
    error: str | None = None
