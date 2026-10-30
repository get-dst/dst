"""The Cube query: what dst resolves a question into, and what Cube compiles.

A ``CubeQuery`` is the REST ``/v1/load`` body, field for field, held as a
pydantic model so every lane emits the same shape and every check reads the
same shape. Its ``canonical()`` form is the identity of an ask: members sorted,
filters sorted, relative date ranges made absolute against a clock, the
defaults Cube would apply written out. ``sha256()`` over that form is the
``query_sha256`` on a receipt, a certified pair and a request log, so the same
ask always hashes the same whatever order a lane wrote it in.

``from_intent`` / ``to_intent`` translate today's ``QueryIntent`` both ways,
member for member, so the two serving lanes can be run side by side on one
intent and compared. Nothing here emits SQL.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.contracts.query_intent import (
    IntentCompare,
    IntentFilter,
    IntentOrder,
    QueryIntent,
    TimeGrain,
)

Granularity = Literal["second", "minute", "hour", "day", "week", "month", "quarter", "year"]
Direction = Literal["asc", "desc"]

# Cube's own default page: a query without ``limit`` returns this many rows.
DEFAULT_LIMIT = 10_000
DEFAULT_TIMEZONE = "UTC"

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_YEAR = re.compile(r"^\d{4}$")
_LAST_N = re.compile(r"^last (\d+) (day|week|month|quarter|year)s?$")

# QueryIntent operator → Cube operator. ``in`` is Cube's ``equals`` with several
# values; ``like`` is ``contains`` (Cube has no wildcard operator).
_OP_TO_CUBE: dict[str, str] = {
    "=": "equals",
    "!=": "notEquals",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "in": "equals",
    "like": "contains",
}
_CUBE_TO_OP: dict[str, str] = {
    "equals": "=",
    "notEquals": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "like",
    "afterOrOnDate": ">=",
    "beforeOrOnDate": "<=",
    "afterDate": ">",
    "beforeDate": "<",
}


class CubeFilter(BaseModel):
    """One filter: a member clause (``member``/``operator``/``values``) or a
    boolean group (``and`` / ``or`` over nested filters). The wire keys are
    Cube's; ``and_``/``or_`` are the Python names."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    member: str | None = None
    operator: str | None = None
    values: list[str] | None = None
    and_: list[CubeFilter] | None = Field(default=None, alias="and")
    or_: list[CubeFilter] | None = Field(default=None, alias="or")

    @field_validator("values", mode="before")
    @classmethod
    def _as_strings(cls, v: object) -> object:
        # Cube takes filter values as strings on the wire; a bool or number
        # written by a lane is the same value spelled Cube's way.
        if isinstance(v, list):
            return [_value_text(x) for x in v]
        if v is None:
            return None
        return [_value_text(v)]

    def wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    def canonical(self) -> dict[str, Any]:
        if self.and_ is not None:
            return {"and": sorted((f.canonical() for f in self.and_), key=_sort_key)}
        if self.or_ is not None:
            return {"or": sorted((f.canonical() for f in self.or_), key=_sort_key)}
        out: dict[str, Any] = {"member": self.member, "operator": self.operator}
        if self.values is not None:
            # Set semantics for equals/notEquals; order kept where it means something.
            out["values"] = (
                sorted(self.values) if self.operator in {"equals", "notEquals"} else self.values
            )
        return out


class TimeDimension(BaseModel):
    """A time dimension clause: the member, an optional bucket, and a window —
    a ``dateRange`` (``[start, end]`` inclusive, or a Cube relative phrase) or a
    ``compareDateRange`` (several windows, one result column set per window)."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    granularity: Granularity | None = None
    dateRange: str | list[str] | None = None  # noqa: N815 — Cube's wire name
    compareDateRange: list[str | list[str]] | None = None  # noqa: N815

    def wire(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def canonical(self, today: date | None) -> dict[str, Any]:
        out: dict[str, Any] = {"dimension": self.dimension}
        if self.granularity is not None:
            out["granularity"] = self.granularity
        if self.dateRange is not None:
            out["dateRange"] = absolute_range(self.dateRange, today)
        if self.compareDateRange is not None:
            out["compareDateRange"] = [absolute_range(r, today) for r in self.compareDateRange]
        return out


class CubeQuery(BaseModel):
    """The ``/v1/load`` body. ``snapshot`` is dst's, not Cube's: a level
    metric's reading inside the window (kept so the ask's identity carries it,
    never sent on the wire)."""

    model_config = ConfigDict(extra="forbid")

    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    timeDimensions: list[TimeDimension] = Field(default_factory=list)  # noqa: N815
    filters: list[CubeFilter] = Field(default_factory=list)
    order: list[tuple[str, Direction]] = Field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    timezone: str | None = None
    ungrouped: bool | None = None
    total: bool | None = None
    snapshot: Literal["first", "last"] | None = None

    @field_validator("order", mode="before")
    @classmethod
    def _order_pairs(cls, v: object) -> object:
        # Cube accepts ``{"m": "desc"}`` or ``[["m", "desc"]]``; both read the
        # same, and so does a list holding one such object (or Cube's own
        # normalised ``{"id": m, "desc": bool}``) per key. Anything else is
        # left for the schema to refuse.
        if isinstance(v, dict):
            return [(k, d) for k, d in v.items()]
        if isinstance(v, list):
            return [_order_pair(x) for x in v]
        return v

    def wire(self) -> dict[str, Any]:
        """The body Cube receives: nothing dst-only, nothing unset."""
        out: dict[str, Any] = {}
        if self.measures:
            out["measures"] = list(self.measures)
        if self.dimensions:
            out["dimensions"] = list(self.dimensions)
        if self.segments:
            out["segments"] = list(self.segments)
        if self.timeDimensions:
            out["timeDimensions"] = [t.wire() for t in self.timeDimensions]
        if self.filters:
            out["filters"] = [f.wire() for f in self.filters]
        if self.order:
            out["order"] = [[m, d] for m, d in self.order]
        for key in ("limit", "offset", "timezone", "ungrouped", "total"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def members(self) -> list[str]:
        """Every member the query names, once, in wire order."""
        seen: list[str] = []
        for name in (
            *self.measures,
            *self.dimensions,
            *self.segments,
            *(t.dimension for t in self.timeDimensions),
            *_filter_members(self.filters),
            *(m for m, _ in self.order),
        ):
            if name not in seen:
                seen.append(name)
        return seen

    def canonical(self, today: date | None = None) -> dict[str, Any]:
        """The identity form: sorted members and filters, absolute ISO windows
        (relative phrases resolved against ``today``, the lens clock), and the
        defaults Cube would apply written out (timezone, limit, order)."""
        return {
            "measures": sorted(self.measures),
            "dimensions": sorted(self.dimensions),
            "segments": sorted(self.segments),
            "timeDimensions": sorted(
                (t.canonical(today) for t in self.timeDimensions), key=_sort_key
            ),
            "filters": sorted((f.canonical() for f in self.filters), key=_sort_key),
            "order": [[m, d] for m, d in self.order],
            "limit": DEFAULT_LIMIT if self.limit is None else self.limit,
            "offset": self.offset or 0,
            "timezone": self.timezone or DEFAULT_TIMEZONE,
            "ungrouped": bool(self.ungrouped),
            "total": bool(self.total),
            "snapshot": self.snapshot,
        }

    def sha256(self, today: date | None = None) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical(today), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


BindingKind = Literal["member", "value", "period", "segment", "definition", "supplied"]


class Binding(BaseModel):
    """One word of the question bound to one thing in the query: the ledger
    line a receipt prints and a check reads."""

    model_config = ConfigDict(extra="forbid")

    word: str
    kind: BindingKind
    target: str
    source: str = Field(
        description="where the binding came from: certified, declared, dictionary, "
        "alias, decider, supplied, …"
    )


class Candidate(BaseModel):
    """A lane's answer to a question: the query, the ledger of how each word
    got there, which lane, the decisions it took and what they cost."""

    model_config = ConfigDict(extra="forbid")

    query: CubeQuery
    ledger: list[Binding] = Field(default_factory=list)
    lane: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    spend: dict[str, float] = Field(default_factory=dict)


# ── windows ─────────────────────────────────────────────────────────────────


def absolute_range(window: str | list[str], today: date | None) -> list[str] | str:
    """A window as ``[start, end]`` ISO days, both inclusive. A Cube relative
    phrase (``today``, ``last 7 days``, ``this month``, ``last quarter``, …)
    resolves against ``today``; without a clock it is kept as written, since a
    guess would be a different ask. A single ISO month or year widens to its
    days; an already absolute pair is normalised."""
    if isinstance(window, list):
        return [_day(window[0]), _day(window[-1], end=True)]
    text = window.strip().lower()
    if _ISO_DAY.match(text):
        return [text, text]
    if _ISO_MONTH.match(text) or _ISO_YEAR.match(text):
        return [_day(text), _day(text, end=True)]
    if today is None:
        return window
    if text == "today":
        return [today.isoformat(), today.isoformat()]
    if text == "yesterday":
        day = today - timedelta(days=1)
        return [day.isoformat(), day.isoformat()]
    if m := _LAST_N.match(text):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            start = today - timedelta(days=n - 1)
            return [start.isoformat(), today.isoformat()]
        # Whole calendar units before the current one, Cube's reading.
        first, last = _calendar_units(today, unit, -n, -1)
        return [first.isoformat(), last.isoformat()]
    for prefix, offset in (("this ", 0), ("last ", -1)):
        if text.startswith(prefix) and text[len(prefix) :] in {"week", "month", "quarter", "year"}:
            first, last = _calendar_units(today, text[len(prefix) :], offset, offset)
            return [first.isoformat(), last.isoformat()]
    return window


def _calendar_units(
    today: date, unit: str, first_offset: int, last_offset: int
) -> tuple[date, date]:
    """The span from the calendar unit ``first_offset`` units from today's to
    the one ``last_offset`` units from it (0 = today's own)."""

    def start(offset: int) -> date:
        if unit == "week":
            monday = today - timedelta(days=today.weekday())
            return monday + timedelta(weeks=offset)
        if unit == "month":
            return _add_months(today.replace(day=1), offset)
        if unit == "quarter":
            q_first = today.replace(day=1, month=3 * ((today.month - 1) // 3) + 1)
            return _add_months(q_first, 3 * offset)
        return today.replace(month=1, day=1).replace(year=today.year + offset)

    first = start(first_offset)
    after = start(last_offset + 1)
    return first, after - timedelta(days=1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return day.replace(year=index // 12, month=index % 12 + 1)


def _day(text: str, *, end: bool = False) -> str:
    """An ISO day; a month or year reads as its first (or last) day."""
    text = text.strip()[:10]
    if _ISO_MONTH.match(text):
        y, m = int(text[:4]), int(text[5:7])
        return date(y, m, calendar.monthrange(y, m)[1] if end else 1).isoformat()
    if _ISO_YEAR.match(text):
        return f"{text}-12-31" if end else f"{text}-01-01"
    return text


# ── QueryIntent ↔ CubeQuery ─────────────────────────────────────────────────


class ViewMembers(BaseModel):
    """What ``from_intent`` needs to know about the view an intent names: the
    members it may call and which of them is the clock. ``LensModel`` and
    ``MetaDoc`` hand this over; a bare translation (no model) qualifies names
    by the view alone."""

    model_config = ConfigDict(extra="forbid")

    view: str
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    time_dimensions: list[str] = Field(default_factory=list)
    clock: str | None = Field(default=None, description="the time dimension windows bind to")


class UnknownMember(ValueError):
    """An intent names a metric, dimension or definition the view does not hold."""

    def __init__(self, kind: str, name: str, view: str) -> None:
        self.kind, self.name, self.view = kind, name, view
        super().__init__(f"{kind} '{name}' is not a member of view '{view}'")


def _qualify(name: str, view: str) -> str:
    return name if "." in name else f"{view}.{name}"


def _check(name: str, known: list[str], kind: str, view: str) -> str:
    if known and name not in known:
        raise UnknownMember(kind, name, view)
    return name


def from_intent(
    intent: QueryIntent,
    model: ViewMembers | None = None,
    *,
    today: date | None = None,
) -> CubeQuery:
    """The intent as a Cube query over its entity's view, member for member.

    Metrics are measures, dimensions are dimensions, definitions are segments,
    a grain and the time filters become one ``timeDimensions`` clause on the
    view's clock, ``compare`` becomes ``compareDateRange`` (the ask's own
    window first), ``fields`` becomes an ungrouped projection. A filter on the
    clock with a date value is the window; every other filter is a filter.
    Raises ``UnknownMember`` when the model is given and a name is not in it.
    """
    view = intent.entity or (model.view if model else None)
    if not view:
        raise ValueError("an intent needs an entity (the view) to become a Cube query")
    known = model or ViewMembers(view=view)
    measures = [_check(_qualify(m, view), known.measures, "metric", view) for m in intent.metrics]
    dimensions = [
        _check(_qualify(d, view), known.dimensions, "dimension", view) for d in intent.dimensions
    ]
    segments = [
        _check(_qualify(d, view), known.segments, "definition", view) for d in intent.definitions
    ]
    fields = [_check(_qualify(f, view), known.dimensions, "field", view) for f in intent.fields]
    clock = known.clock or (known.time_dimensions[0] if known.time_dimensions else None)
    window: list[str | None] = [None, None]
    filters: list[CubeFilter] = []
    for f in intent.filters:
        member = _qualify(f.field, view)
        if _is_window_filter(f, member, known, clock):
            clock = clock or member
            _bind_window(window, f)
            continue
        filters.append(
            CubeFilter(
                member=member,
                operator=_OP_TO_CUBE[f.op],
                values=f.value if isinstance(f.value, list) else [f.value],
            )
        )
    time_dimensions: list[TimeDimension] = []
    bounded = window[0] is not None or window[1] is not None
    if intent.grain or bounded or intent.compare:
        if clock is None:
            raise UnknownMember("time dimension", "(clock)", view)
        clause = TimeDimension(dimension=clock, granularity=_granularity(intent.grain))
        if window[0] is not None and window[1] is not None:
            clause.dateRange = [window[0], window[1]]
        elif bounded:
            # Cube refuses a dateRange with an empty end; a window open on one
            # side is an inclusive date filter on the clock instead.
            if window[0] is not None:
                operator, bound = "afterOrOnDate", window[0]
            else:
                operator, bound = "beforeOrOnDate", str(window[1])
            filters.append(CubeFilter(member=clock, operator=operator, values=[bound]))
        if intent.compare:
            if clause.dateRange is None:
                raise ValueError(
                    f"compare: {intent.compare.to} needs the ask's own window bounded on "
                    "both ends on the clock"
                )
            clause.compareDateRange = [clause.dateRange, _compare_window(window, intent.compare)]
            clause.dateRange = None
        time_dimensions.append(clause)
    return CubeQuery(
        measures=measures,
        dimensions=dimensions + fields,
        segments=segments,
        timeDimensions=time_dimensions,
        filters=filters,
        order=[(_qualify(o.field, view), o.dir) for o in intent.order_by],
        limit=intent.limit,
        ungrouped=True if fields else None,
        snapshot=intent.snapshot,
    )


def to_intent(query: CubeQuery, view: str | None = None) -> QueryIntent:
    """The Cube query as today's intent: the view is the entity, member names
    lose their view prefix, the time clause becomes grain + bounds, segments
    become definitions, ``compareDateRange`` becomes ``compare``."""
    view = view or _view_of(query)
    prefix = view + "."

    def strip(name: str) -> str:
        return name[len(prefix) :] if name.startswith(prefix) else name

    filters = [
        IntentFilter(field=strip(m), op=op, value=value) for m, op, value in _flat(query.filters)
    ]
    grain: TimeGrain | None = None
    compare: IntentCompare | None = None
    for t in query.timeDimensions:
        if t.granularity in {"hour", "day", "week", "month", "quarter", "year"}:
            grain = t.granularity  # type: ignore[assignment]
        window = t.dateRange
        if t.compareDateRange:
            window = t.compareDateRange[0]
            compare = IntentCompare(to=_compare_text(t.compareDateRange[1]))
        if isinstance(window, list):
            if window[0]:
                filters.append(IntentFilter(field=strip(t.dimension), op=">=", value=window[0]))
            if window[-1]:
                filters.append(IntentFilter(field=strip(t.dimension), op="<=", value=window[-1]))
    fields = [strip(d) for d in query.dimensions] if query.ungrouped else []
    return QueryIntent(
        entity=view,
        metrics=[strip(m) for m in query.measures],
        dimensions=[] if query.ungrouped else [strip(d) for d in query.dimensions],
        definitions=[strip(s) for s in query.segments],
        grain=grain,
        filters=filters,
        order_by=[IntentOrder(field=strip(m), dir=d) for m, d in query.order],
        limit=query.limit,
        fields=fields,
        snapshot=query.snapshot,
        compare=compare,
    )


def _view_of(query: CubeQuery) -> str:
    for name in query.members():
        if "." in name:
            return name.split(".", 1)[0]
    raise ValueError("a Cube query with no qualified member names no view")


def _is_window_filter(f: IntentFilter, member: str, known: ViewMembers, clock: str | None) -> bool:
    if f.op not in {">", ">=", "<", "<=", "="}:
        return False
    if member == clock or member in known.time_dimensions:
        return True
    # No model: a bound on an ISO date is a window on that member.
    return (
        not known.time_dimensions
        and isinstance(f.value, str)
        and bool(_ISO_DAY.match(f.value) or _ISO_MONTH.match(f.value) or _ISO_YEAR.match(f.value))
    )


def _bind_window(window: list[str | None], f: IntentFilter) -> None:
    value = str(f.value)
    if f.op in {">=", ">"}:
        day = _day(value)
        window[0] = (
            (date.fromisoformat(day) + timedelta(days=1)).isoformat() if f.op == ">" else day
        )
    elif f.op in {"<=", "<"}:
        day = _day(value, end=True)
        window[1] = (
            (date.fromisoformat(day) - timedelta(days=1)).isoformat() if f.op == "<" else day
        )
    else:
        window[0], window[1] = _day(value), _day(value, end=True)


def _granularity(grain: TimeGrain | None) -> Granularity | None:
    return grain


def _compare_window(window: list[str | None], compare: IntentCompare) -> list[str]:
    to = compare.to.strip()
    if to not in {"previous_period", "previous_year"}:
        if "/" in to:
            a, b = to.split("/", 1)
            return [_day(a), _day(b, end=True)]
        return [_day(to), _day(to, end=True)]
    if window[0] is None or window[1] is None:
        raise ValueError(
            f"compare: {to} needs the ask's own window bounded on both ends on the clock"
        )
    start, end = date.fromisoformat(window[0]), date.fromisoformat(window[1])
    if to == "previous_year":
        return [_shift_year(start).isoformat(), _shift_year(end).isoformat()]
    length = (end - start).days + 1
    return [(start - timedelta(days=length)).isoformat(), (start - timedelta(days=1)).isoformat()]


def _shift_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # Feb 29
        return day.replace(year=day.year - 1, day=28)


def _compare_text(window: str | list[str]) -> str:
    if isinstance(window, list):
        return f"{window[0]}/{window[-1]}"
    return window


def _flat(filters: list[CubeFilter]) -> list[tuple[str, Any, Any]]:
    """Member clauses as ``(member, intent op, value)``; a top-level ``and``
    flattens, an ``or`` has no intent shape and is dropped."""
    out: list[tuple[str, Any, Any]] = []
    for f in filters:
        if f.and_ is not None:
            out.extend(_flat(f.and_))
        elif f.member and f.operator in _CUBE_TO_OP:
            values = f.values or []
            op = _CUBE_TO_OP[f.operator]
            if op == "=" and len(values) > 1:
                out.append((f.member, "in", values))
            else:
                out.append((f.member, op, values[0] if values else None))
    return out


def _filter_members(filters: list[CubeFilter]) -> list[str]:
    out: list[str] = []
    for f in filters:
        if f.member:
            out.append(f.member)
        for group in (f.and_, f.or_):
            if group:
                out.extend(_filter_members(group))
    return out


def _order_pair(item: object) -> object:
    """One order entry as ``(member, direction)``, or as written when it
    names no single member and direction."""
    if not isinstance(item, dict):
        return item
    member = item.get("id", item.get("member"))
    if isinstance(member, str):
        if isinstance(item.get("desc"), bool) and len(item) == 2:
            return (member, "desc" if item["desc"] else "asc")
        direction = item.get("direction", item.get("dir"))
        if isinstance(direction, str) and len(item) == 2:
            return (member, direction)
    if len(item) == 1:
        return next(iter(item.items()))
    return item


def _value_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sort_key(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True)
