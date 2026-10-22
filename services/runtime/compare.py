"""Period comparison: one intent, two windows, one statement.

``QueryIntent.compare`` asks for every metric twice: over the intent's own
date window, and over a comparison window derived from it (the previous period
of equal length, the same period a year earlier, or an explicit range). Each
window is compiled by ``compile_intent`` on its own, so a level metric reads
each window's latest snapshot and a flow metric sums each window's rows,
exactly as two separate asks would. The two results are joined on the
intent's grouping keys and every metric gains three columns:
``<metric>_compare``, ``<metric>_delta`` (value minus compare value) and
``<metric>_delta_pct`` (the delta over the absolute compare value; null when
that is zero).

The comparison window comes from the intent's own filters on the metrics' time
field. An intent without such a window has no period to compare against, and
that is a ``CompileError`` naming the fix, never a guess at "this month".

With a ``grain`` the buckets of the two windows are matched by shifting the
current bucket back by the distance between the windows, so that distance must
be a whole number of buckets: calendar months for month, quarter and year
grains, days for hour, day and week grains (whole weeks for week). A window and
grain that cannot line up are refused rather than matched approximately.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import sqlglot
from sqlglot import exp

from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.semantic_model import Entity, SemanticModel
from services.runtime.compiler import (
    CompileError,
    _member_type,
    _physical_type,
    _resolve_entity,
    compile_intent,
)
from services.runtime.identifiers import quote_ident

EXPLICIT_RANGE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2})/(\d{4})-(\d{2})-(\d{2}))?$")
_CURRENT = "dst_current"
_COMPARE = "dst_compare"
_CURRENT_ROWS = "dst_current_rows"
_MATCHED = "dst_matched"
_MONTH_GRAINS = {"month": 1, "quarter": 3, "year": 12}


@dataclass(frozen=True)
class Window:
    """A half-open day range: ``start`` inclusive, ``end`` exclusive."""

    start: date
    end: date

    def month_aligned(self) -> bool:
        return self.start.day == 1 and self.end.day == 1

    def months(self) -> int:
        return (self.end.year - self.start.year) * 12 + self.end.month - self.start.month


def add_months(day: date, months: int) -> date:
    """*day* moved by whole calendar months, clipped to the target month's end."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, min(day.day, (nxt - timedelta(days=1)).day))


def _day(value: object, field: str) -> date:
    text = str(value).strip() if value is not None else ""
    head, _, rest = text.replace("T", " ").partition(" ")
    try:
        parsed = date.fromisoformat(head)
    except ValueError:
        raise CompileError(
            f"compare reads the window on '{field}' as dates, and {value!r} is not one "
            "— bound it with YYYY-MM-DD values"
        ) from None
    if rest.strip() and not rest.strip().startswith("00:00"):
        raise CompileError(
            f"compare reads windows in whole days, and {value!r} on '{field}' carries a "
            "time of day — bound the window at midnight"
        )
    return parsed


def intent_window(filters: Sequence[IntentFilter], field: str) -> Window:
    """The half-open day window the filters on one time field bound, refusing a
    window open on either end: a comparison needs a period with a length."""
    lo: date | None = None
    hi: date | None = None
    for f in filters:
        if f.op not in (">=", ">", "<", "<=", "="):
            raise CompileError(
                f"compare needs a range on '{field}', and '{f.op}' does not bound one — "
                "write the window as >= start and < end"
            )
        day = _day(f.value, field)
        start = {">=": day, ">": day + timedelta(days=1), "=": day}.get(f.op)
        end = {"<": day, "<=": day + timedelta(days=1), "=": day + timedelta(days=1)}.get(f.op)
        if start is not None:
            lo = start if lo is None else max(lo, start)
        if end is not None:
            hi = end if hi is None else min(hi, end)
    if lo is None or hi is None:
        raise CompileError(
            f"compare needs the intent's window bounded on both ends on '{field}' — add "
            f"filters such as {field} >= 2026-09-01 and {field} < 2026-10-01"
        )
    if hi <= lo:
        raise CompileError(f"the window on '{field}' is empty, so there is nothing to compare")
    return Window(lo, hi)


def explicit_window(text: str) -> Window:
    """``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD/YYYY-MM-DD`` (both days inclusive)."""
    m = EXPLICIT_RANGE.match(text.strip())
    if m is None:
        raise CompileError(
            f"compare.to '{text}' is not previous_period, previous_year or a range "
            "(YYYY, YYYY-MM, or YYYY-MM-DD/YYYY-MM-DD)"
        )
    year, month, day, year2, month2, day2 = m.groups()
    try:
        if day is not None:
            start = date(int(year), int(month), int(day))
            end = date(int(year2), int(month2), int(day2)) + timedelta(days=1)
        elif month is not None:
            start = date(int(year), int(month), 1)
            end = add_months(start, 1)
        else:
            start, end = date(int(year), 1, 1), date(int(year) + 1, 1, 1)
    except ValueError as exc:
        raise CompileError(f"compare.to '{text}' is not a real date range: {exc}") from None
    if end <= start:
        raise CompileError(f"compare.to '{text}' ends before it starts")
    return Window(start, end)


@dataclass(frozen=True)
class Shift:
    """How far back the comparison window sits: calendar months or days."""

    months: int = 0
    days: int = 0

    def apply(self, w: Window) -> Window:
        if self.months:
            return Window(add_months(w.start, -self.months), add_months(w.end, -self.months))
        return Window(w.start - timedelta(days=self.days), w.end - timedelta(days=self.days))


def comparison(target: str, current: Window, grain: str | None) -> tuple[Window, Shift]:
    """The comparison window and the shift that maps a current bucket onto it."""
    monthly = grain in _MONTH_GRAINS
    if target == "previous_year":
        # Whole weeks a year back keep each week on the same weekday boundary.
        shift = Shift(days=364) if grain == "week" else Shift(months=12)
        return shift.apply(current), shift
    if target == "previous_period":
        if current.month_aligned() and grain not in ("hour", "day", "week"):
            shift = Shift(months=current.months())
        elif monthly:
            raise CompileError(
                f"a {grain}-by-{grain} comparison needs a window that starts and ends on "
                f"a month boundary — {current.start} to {current.end} does not"
            )
        else:
            shift = Shift(days=(current.end - current.start).days)
        _check_grain(shift, grain)
        return shift.apply(current), shift
    other = explicit_window(target)
    if monthly:
        if not (other.month_aligned() and current.month_aligned()):
            raise CompileError(
                f"a {grain}-by-{grain} comparison needs both windows on month boundaries"
            )
        shift = Shift(
            months=(current.start.year - other.start.year) * 12
            + current.start.month
            - other.start.month
        )
    else:
        shift = Shift(days=(current.start - other.start).days)
    if grain is not None:
        _check_grain(shift, grain)
    return other, shift


def _check_grain(shift: Shift, grain: str | None) -> None:
    if grain in _MONTH_GRAINS and shift.months % _MONTH_GRAINS[grain]:
        raise CompileError(
            f"the comparison window sits {shift.months} months back, which is not a whole "
            f"number of {grain}s — the buckets cannot line up"
        )
    if grain == "week" and shift.days % 7:
        raise CompileError(
            f"the comparison window sits {shift.days} days back, which is not a whole "
            "number of weeks — the weekly buckets cannot line up"
        )


def _window_filters(
    intent: QueryIntent, entity: Entity, time_fields: set[str]
) -> list[IntentFilter]:
    out = []
    for f in intent.filters:
        owner, _, col = f.field.rpartition(".")
        if col in time_fields and owner in ("", entity.name):
            out.append(f)
    return out


# The BigQuery types a bucket of a date or moment column can be.
_BQ_BUCKET_TYPES = frozenset({"DATE", "DATETIME", "TIMESTAMP"})


def bucket_type(
    entity: Entity, field: str, column_types: Mapping[str, Mapping[str, str]] | None
) -> str:
    """What a bucket of *field* is on BigQuery: DATE, DATETIME or TIMESTAMP.

    DATE_TRUNC keeps its argument's type there, and each type has its own
    arithmetic, so the warehouse's own type from the stored profile decides.
    Without one the declared type does: ``timestamp`` reads as TIMESTAMP,
    anything else as DATE."""
    physical = (_physical_type(column_types, entity, field) or "").upper()
    if physical in _BQ_BUCKET_TYPES:
        return physical
    return "TIMESTAMP" if _member_type(entity, field) == "timestamp" else "DATE"


def _shifted(
    col: str, shift: Shift, dialect: str, *, forward: bool = False, kind: str = "DATE"
) -> str:
    """``col`` moved back (or *forward*) by *shift* in the dialect's own date
    arithmetic. *kind* is the bucket's BigQuery type (``bucket_type``): there
    DATE_SUB takes a TIMESTAMP only in parts up to DAY, so each type gets its
    own function, and a TIMESTAMP moves by calendar months through DATETIME,
    in UTC, the zone its DATE_TRUNC bucket was cut in."""
    unit, amount = ("MONTH", shift.months) if shift.months else ("DAY", shift.days)
    this = sqlglot.parse_one(col, read=dialect)
    step = {"expression": exp.Literal.number(amount), "unit": exp.Var(this=unit)}
    if dialect != "bigquery" or kind == "DATE":
        node: exp.Expr = (exp.DateAdd if forward else exp.DateSub)(this=this, **step)
    elif kind == "DATETIME":
        node = (exp.DatetimeAdd if forward else exp.DatetimeSub)(this=this, **step)
    elif shift.months:
        as_datetime = exp.cast(this, exp.DataType.build("DATETIME", dialect="bigquery"))
        moved = (exp.DatetimeAdd if forward else exp.DatetimeSub)(this=as_datetime, **step)
        node = exp.cast(moved, exp.DataType.build("TIMESTAMP", dialect="bigquery"))
    else:
        node = (exp.TimestampAdd if forward else exp.TimestampSub)(this=this, **step)
    return node.sql(dialect=dialect)


@dataclass(frozen=True)
class ComparisonPlan:
    """What a comparison reads: the bounded time fields, the filters that bound
    them, the intent's own window, the comparison window, and the shift between."""

    entity: Entity
    time_fields: tuple[str, ...]
    window_filters: tuple[IntentFilter, ...]
    fields: tuple[str, ...]
    current: Window
    target: Window
    shift: Shift


def plan_comparison(intent: QueryIntent, model: SemanticModel) -> ComparisonPlan:
    """The two windows of ``intent.compare``, or a CompileError naming why not."""
    assert intent.compare is not None
    if not intent.metrics or intent.fields:
        raise CompileError("compare reads metrics: name at least one, and no listing fields")
    entity = _resolve_entity(model, intent.entity, intent.metrics)
    by_name = {m.name: m for m in entity.metrics}
    unknown = [m for m in intent.metrics if m not in by_name]
    if unknown:
        raise CompileError(f"unknown metric '{unknown[0]}'")
    time_fields = sorted(
        {f for m in intent.metrics if (f := by_name[m].agg_time_field or entity.default_time_field)}
    )
    window_filters = _window_filters(intent, entity, set(time_fields))
    if not window_filters:
        named = ", ".join(time_fields) or "the entity's time field"
        raise CompileError(
            f"compare needs the intent's own date window and this intent has none — add "
            f"filters bounding {named} (>= start, < end); without a window there is no "
            "period to compare against"
        )
    fields = sorted({f.field for f in window_filters})
    current = intent_window(window_filters, fields[0])
    for other in fields[1:]:
        if intent_window([f for f in window_filters if f.field == other], other) != current:
            raise CompileError(
                f"the window on '{other}' differs from the one on '{fields[0]}' — compare "
                "derives one comparison window, so bound both fields alike"
            )
    target, shift = comparison(intent.compare.to, current, intent.grain)
    return ComparisonPlan(
        entity, tuple(time_fields), tuple(window_filters), tuple(fields), current, target, shift
    )


def comparison_reading(intent: QueryIntent, model: SemanticModel) -> tuple[str, str] | None:
    """``(time field, reading)`` naming the comparison window, for the ledger;
    None when the intent compares nothing or the comparison does not compile."""
    if intent.compare is None:
        return None
    try:
        plan = plan_comparison(intent, model)
    except CompileError:
        return None
    last = plan.target.end - timedelta(days=1)
    return (
        plan.fields[0].rpartition(".")[2],
        f"compared with {plan.target.start.isoformat()} to {last.isoformat()} "
        f"({intent.compare.to})",
    )


def compile_comparison(
    intent: QueryIntent,
    model: SemanticModel,
    *,
    domains: Mapping[str, Sequence[str]] | None = None,
    column_types: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """The intent's metrics over its own window beside the comparison window."""
    plan = plan_comparison(intent, model)
    entity, time_fields, target, shift = plan.entity, plan.time_fields, plan.target, plan.shift
    window_filters, fields = list(plan.window_filters), plan.fields
    inner = intent.model_copy(update={"compare": None, "order_by": [], "limit": None})
    kept = [f for f in intent.filters if f not in window_filters]
    compare_filters = [
        *kept,
        *(
            g
            for name in fields
            for g in (
                IntentFilter(field=name, op=">=", value=target.start.isoformat()),
                IntentFilter(field=name, op="<", value=target.end.isoformat()),
            )
        ),
    ]
    current_sql = compile_intent(inner, model, domains=domains, column_types=column_types)
    compare_sql = compile_intent(
        inner.model_copy(update={"filters": compare_filters}),
        model,
        domains=domains,
        column_types=column_types,
    )

    dialect = model.dialect

    def q(name: str) -> str:
        return quote_ident(name, dialect)

    c, p = q(_CURRENT), q(_COMPARE)
    keys: list[str] = []
    kind = "DATE"
    if intent.grain:
        field = next(iter(time_fields), entity.default_time_field)
        keys.append(f"{field}_{intent.grain}")
        kind = bucket_type(entity, field or "", column_types)
    keys += list(intent.dimensions)
    conds = []
    for i, key in enumerate(keys):
        cur = f"{c}.{q(key)}"
        if i == 0 and intent.grain:
            cur = _shifted(cur, shift, dialect, kind=kind)
        conds.append(f"{cur} IS NOT DISTINCT FROM {p}.{q(key)}")
    on = " AND ".join(conds) or "1 = 1"
    cols = [f"{c}.{q(k)} AS {q(k)}" for k in keys]
    rest = [f"{_unshift(p, keys, k, shift, intent.grain, dialect, kind)} AS {q(k)}" for k in keys]
    for m in intent.metrics:
        now, then = f"{c}.{q(m)}", f"{p}.{q(m)}"
        cols += [
            f"{now} AS {q(m)}",
            f"{then} AS {q(m + '_compare')}",
            f"{now} - {then} AS {q(m + '_delta')}",
            f"CAST({now} - {then} AS DOUBLE) / NULLIF(ABS({then}), 0) AS {q(m + '_delta_pct')}",
        ]
        rest += [
            f"NULL AS {q(m)}",
            f"{then} AS {q(m + '_compare')}",
            f"NULL AS {q(m + '_delta')}",
            f"NULL AS {q(m + '_delta_pct')}",
        ]
    # Every current row carries a marker, so a comparison row the join found
    # no current row for is the one whose marker came back NULL.
    hit = q(_MATCHED)
    sql = (
        f"WITH {c} AS (SELECT 1 AS {hit}, {q(_CURRENT_ROWS)}.* FROM ({current_sql}) AS "
        f"{q(_CURRENT_ROWS)}), {p} AS ({compare_sql}) "
        f"SELECT {', '.join(cols)} FROM {c} LEFT JOIN {p} ON {on}"
    )
    if keys:
        # A group present only in the comparison window (a segment that went to
        # nothing) still shows, with its compare value and no current one. An
        # anti-join, written as a LEFT JOIN: BigQuery refuses NOT EXISTS whose
        # condition is not a plain equality, and IS NOT DISTINCT FROM (which
        # keeps a NULL group matched to its NULL counterpart) is not one.
        sql += (
            f" UNION ALL SELECT {', '.join(rest)} FROM {p} LEFT JOIN {c} ON {on} "
            f"WHERE {c}.{hit} IS NULL"
        )
    selected = set(keys) | {
        f"{m}{suffix}"
        for m in intent.metrics
        for suffix in ("", "_compare", "_delta", "_delta_pct")
    }
    if intent.order_by:
        parts = []
        for o in intent.order_by:
            if o.field not in selected:
                raise CompileError(
                    f"a comparison orders only by what it selects — '{o.field}' is not selected"
                )
            parts.append(f"{q(o.field)} {o.dir.upper()}")
        sql += " ORDER BY " + ", ".join(parts)
    if intent.limit:
        sql += f" LIMIT {int(intent.limit)}"
    try:
        return sqlglot.transpile(sql, read=dialect, write=dialect)[0]
    except Exception as exc:  # noqa: BLE001 — any parse/transpile failure is a compile error
        raise CompileError(f"could not transpile the comparison to {dialect}: {exc}") from exc


def _unshift(
    p: str, keys: list[str], key: str, shift: Shift, grain: str | None, dialect: str, kind: str
) -> str:
    """A comparison-only row's key as the current window would label it."""
    col = f"{p}.{quote_ident(key, dialect)}"
    if not (grain and key == keys[0]):
        return col
    return _shifted(col, shift, dialect, forward=True, kind=kind)
