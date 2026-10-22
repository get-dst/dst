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


class IntentCompare(BaseModel):
    to: str = PField(
        description="the comparison window: `previous_period` (the window of equal "
        "length just before the intent's own), `previous_year` (the same window a year "
        "earlier), or an explicit range — YYYY, YYYY-MM, or YYYY-MM-DD/YYYY-MM-DD with "
        "both days inclusive",
    )


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
    definitions_suggested: list[str] = PField(
        default_factory=list,
        description="definitions the generator chose that the question never named (no "
        "term, alias or `about` metric in its words) — recorded on the resolution as "
        "definition_suggested and NOT compiled: a predicate nobody asked for is a "
        "silent filter",
    )
    literals_dropped: list[str] = PField(
        default_factory=list,
        description="literal filters the generator added on a column a selected metric's "
        "own filters (or its CASE conditions) already govern — 'completed visits' as "
        "status = 'completed' beside the metric's status IN ('completed', 'billed'). "
        "The metric's predicate wins, the literal is dropped at compile and recorded "
        "on the ledger as literal_dropped: the extra predicate silently halves a count",
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
    fields: list[str] = PField(
        default_factory=list,
        description="a LISTING's projection: member names shown per row, no aggregation "
        "and no GROUP BY — the typed resolver's shape for 'list / show / which records'",
    )
    snapshot: Literal["first", "last"] | None = PField(
        default=None,
        description="level metrics only: which snapshot inside the window the figure "
        "reads — `last` (the period's end) or `first` (its start: 'headcount at the "
        "beginning of Q3'). Unset, the metric's declared window reading applies "
        "(the last snapshot unless the metric declares `any_in_window`)",
    )
    compare: IntentCompare | None = PField(
        default=None,
        description="run the intent over its own date window AND a comparison window "
        "derived from it; every metric gains <metric>_compare, <metric>_delta and "
        "<metric>_delta_pct columns. Needs the intent's window bounded on both ends on "
        "the metrics' time field — without one the compile is refused",
    )
