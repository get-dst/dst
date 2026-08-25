"""Structured query intent — the metric-layer alternative to raw text-to-SQL.

Instead of emitting SQL, the model emits a QueryIntent (which metrics, dimensions,
filters) chosen *by name* from the lens's semantic model; the compiler turns it into
correct, dialect-specific SQL deterministically. The model can only pick names that
exist, so joins/aggregations/grains/dialect become non-hallucinable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel
from pydantic import Field as PField

FilterOp = Literal["=", "!=", ">", "<", ">=", "<=", "in", "like"]


class IntentFilter(BaseModel):
    field: str
    op: FilterOp = "="
    value: Any = None


class IntentOrder(BaseModel):
    field: str
    dir: Literal["asc", "desc"] = "desc"


TimeGrain = Literal["hour", "day", "week", "month", "quarter", "year"]


class QueryIntent(BaseModel):
    entity: str | None = None
    metrics: list[str] = PField(default_factory=list)
    dimensions: list[str] = PField(default_factory=list)
    definitions: list[str] = PField(
        default_factory=list,
        description="business definitions to apply as governed filters — the compiler "
        "injects each named definition's sql_expr VERBATIM into the WHERE clause. This is "
        "how the enforceable half of a definition reaches a metric-layer answer instead of "
        "being reconstructed (or guessed) by the model — the serialize_layer prompt renders "
        "sql_expr only as prose, so a metric lens could never apply an edited predicate",
    )
    grain: TimeGrain | None = PField(
        default=None,
        description="bucket the entity's time field to this period and group by it — "
        "the whole of 'by month', 'per week', 'daily'. Without it an over-time "
        "question groups on the raw timestamp, which is per-event grain wearing a "
        "per-period costume",
    )
    filters: list[IntentFilter] = PField(default_factory=list)
    order_by: list[IntentOrder] = PField(default_factory=list)
    limit: int | None = None
