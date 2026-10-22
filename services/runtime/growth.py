"""A change over time asked in words, read into a period comparison.

"MoM growth", "how did X change last month", "this quarter vs last quarter",
"compared with the same month last year": each asks for a delta between two
windows, never for the level of one. ``QueryIntent.compare`` computes that
delta deterministically (services/runtime/compare.py); this module decides,
from the question's own words, whether a comparison is asked and which window
it compares against. Nothing here guesses a period:

- two stated periods: the earlier is the comparison window and the later the
  answer's own, whichever is written first, so the delta runs forward in time;
- one stated period: compared with the one before it of equal length, the
  same days a year earlier for a year-on-year word, or, for a calendar
  period read to date ("this month"), the same days of the period before;
- a unit word (MoM, QoQ, YoY, WoW, "week over week") over a window longer
  than the unit is that unit's series, each bucket against the one before;
- no stated period, or only the comparison window ("vs last month" and
  nothing it is compared with): a clarification offering
  the last complete period, never a comparison of an invented window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from services.contracts.query_intent import IntentFilter
from services.contracts.resolution import DecisionRecord
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import Entity, Metric
from services.runtime import timewindow
from services.runtime.compare import Window, add_months, intent_window
from services.runtime.compiler import CompileError, reads_as_level

_WORD = re.compile(r"[a-z0-9]+")
# A change is asked by a growth noun, by "how did / has X change", or by a
# change noun before what it is of or since when ("the change in fee revenue",
# "fee revenue change since August", "the fee revenue increase?"). "Members
# who changed plan" counts an event, not a delta: a bare verb is none. "vs"
# and "compared" ask for one only beside a period (see ``read``): "revenue vs
# cost" compares two figures, not two windows.
_MOVE = (
    r"change[ds]?|increase[ds]?|decrease[ds]?|decline[ds]?|rise|rose|risen|fall|fell|fallen"
    r"|drop(?:ped)?"
)
_NOUN = r"change|changes|increase|decrease|decline|drop|rise|fall"
_CHANGE = re.compile(
    r"\b(?P<g>growth|grew|grown|delta)\b"
    r"|\bhow\s+(?:much\s+)?(?:did|has|have|does|do|is|was|were|had)\b[^?.!;]*?"
    rf"\b(?P<h>{_MOVE}|grow|grows)\b"
    rf"|\b(?P<n>{_NOUN})\s+(?:in|of|from|since|vs\.?|versus|compared|over|last|this|previous|prior)\b"
    rf"|\b(?P<e>{_NOUN})\s*[?.!]?\s*$",
    re.I,
)
_UNIT_WORD = re.compile(r"\b(mom|qoq|yoy|wow)\b", re.I)
_UNIT_OF = {"mom": "month", "qoq": "quarter", "yoy": "year", "wow": "week"}
_OVER = re.compile(
    r"\b(day|week|month|quarter|year)[- ]over[- ](day|week|month|quarter|year)\b", re.I
)
_SAME_LAST_YEAR = re.compile(
    r"\b(?:the\s+)?same\s+(?:day|week|month|quarter|period|time|days)\s+(?:of\s+)?"
    r"(?:last|a|the\s+previous|previous|the\s+prior|prior)\s+year\b",
    re.I,
)
# The words that make the period right after them the comparison window.
_AGAINST = re.compile(
    r"\b(?:vs\.?|versus|compared\s+(?:to|with)|than|from|against)\s+(?:the\s+)?$", re.I
)
# The period before, named relative to the answer's own: "vs the month before".
_BEFORE = re.compile(
    r"\b(?:vs\.?|versus|compared\s+(?:to|with)|than|against)\s+(?:the\s+)?"
    r"(?:(?:day|week|month|quarter|year|period)\s+before|one\s+before|previous\s+one|prior\s+one)\b",
    re.I,
)
_COMPARING = re.compile(r"\b(?:vs\.?|versus|compared?|comparison|difference)\b", re.I)
_NET_NEW = re.compile(r"\bnet\s+new\b", re.I)
# A metric whose own words say it is a change already answers a change.
CHANGE_WORDS = frozenset(
    "growth change changes delta increase decrease diff difference variance movement "
    "movements".split()
)
_ORDER = ("day", "week", "month", "quarter", "half", "year")
_MONTHS_IN = {"month": 1, "quarter": 3, "half": 6, "year": 12}


def stated_unit(question: str) -> str | None:
    """The calendar unit a year-on-year style word names, or None."""
    if m := _UNIT_WORD.search(question):
        return _UNIT_OF[m.group(1).lower()]
    if (m := _OVER.search(question)) and m.group(1).lower() == m.group(2).lower():
        return m.group(1).lower()
    if _SAME_LAST_YEAR.search(question):
        return "year"
    return None


def _outer_spans(text: str) -> list[tuple[int, int]]:
    spans = timewindow.temporal_spans(text)
    return [s for s in spans if not any(o != s and o[0] <= s[0] and s[1] <= o[1] for o in spans)]


def _range(text: str, today: date) -> tuple[date, date] | None:
    ranges = timewindow.window_ranges(timewindow.temporal_terms(text), today)
    return ranges[0] if len(ranges) == 1 else None


def _blank(text: str, a: int, b: int) -> str:
    return text[:a] + " " * (b - a) + text[b:]


def net_new_unmet(question: str, metric_words: set[str]) -> bool:
    """ "net new" asked of metrics none of which is a net figure: new-only
    would be served for a net question."""
    return bool(_NET_NEW.search(question)) and "net" not in metric_words


@dataclass(frozen=True)
class Reading:
    """A comparison the question asks for. ``text`` is the question with the
    comparison window's phrase blanked, so the answer's own window is read
    from it alone; ``against`` the comparison window two stated periods give
    (inclusive days); ``unit`` the unit a year-on-year style word names."""

    text: str
    unit: str | None
    against: tuple[date, date] | None = None


def read(
    question: str, metric_words: set[str], naming: set[str], today: date
) -> Reading | ClarificationRequest | None:
    """The comparison the question asks for, a clarification when it asks one
    it does not place, or None when it asks none. ``metric_words``: the
    chosen metrics' own words — a metric named for a change (`member_change`)
    is the change, and its period an ordinary window. ``naming``: words of
    fields, dimensions and stored values; a change word among them is a name
    ("plan changes" over a `plan_change` field), not a comparison."""
    unit = stated_unit(question)
    changes = [
        (m.group("g") or m.group("h") or m.group("n") or m.group("e")).lower()
        for m in _CHANGE.finditer(question)
    ]
    worded = (
        unit is not None
        or _BEFORE.search(question) is not None
        or any(w not in naming | metric_words for w in changes)
    )
    text = question
    if same := _SAME_LAST_YEAR.search(text):
        text = _blank(text, *same.span())
    spans = [s for s in _outer_spans(text) if _range(text[s[0] : s[1]], today) is not None]
    marked = [s for s in spans if _AGAINST.search(text[: s[0]])]
    if not worded and not marked and not (len(spans) == 2 and _COMPARING.search(question)):
        return None
    if metric_words & CHANGE_WORDS and unit is None and not marked and len(spans) < 2:
        # The metric is the change itself (`net_member_change`): its period is
        # an ordinary window.
        return None
    if len(spans) == 2:
        # A change runs from the earlier period to the later, whichever is
        # written first; two phrases naming one period ("last month vs the
        # previous month") compare it with the period before it.
        first, second = spans
        ra = _range(text[first[0] : first[1]], today)
        rb = _range(text[second[0] : second[1]], today)
        assert ra is not None and rb is not None
        other = first if ra[0] < rb[0] or (ra == rb and first in marked) else second
        against = None if ra == rb else _range(text[other[0] : other[1]], today)
        return Reading(text=_blank(text, *other), unit=unit, against=against)
    if len(spans) == 1 and marked:
        phrase = text[spans[0][0] : spans[0][1]].strip()
        return ClarificationRequest(
            kind="unresolved_slot",
            term="window",
            question=f"The question compares with {phrase} but names no period to compare "
            f"with it. Which period, e.g. '... this month vs {phrase}'?",
            options=[f"this month vs {phrase}"] if "month" in phrase.lower() else [phrase],
        )
    if len(spans) > 2:
        return None  # the window reading asks which period
    return Reading(text=text, unit=unit)


def no_window(unit: str | None, today: date) -> ClarificationRequest:
    """A change asked over no stated period: offer the last complete one."""
    label, before = _last_complete(unit or "month", today)
    return ClarificationRequest(
        kind="unresolved_slot",
        term="window",
        question="The question asks for a change and names no period to measure it over. "
        f"The last complete {unit or 'month'} is {label} — compared with {before}?",
        options=[f"{label} vs {before}"],
    )


def _last_complete(unit: str, today: date) -> tuple[str, str]:
    if unit == "year":
        return str(today.year - 1), str(today.year - 2)
    if unit == "quarter":
        q = (today.month - 1) // 3  # the current quarter, 0-based
        year, q = (today.year, q) if q else (today.year - 1, 4)
        prev_year, prev_q = (year, q - 1) if q > 1 else (year - 1, 4)
        return f"Q{q} {year}", f"Q{prev_q} {prev_year}"
    if unit in ("week", "day"):
        monday = today - timedelta(days=today.weekday() + 7)
        prev = monday - timedelta(days=7)
        return (
            f"{monday.isoformat()} to {(monday + timedelta(days=6)).isoformat()}",
            f"{prev.isoformat()} to {(prev + timedelta(days=6)).isoformat()}",
        )
    last = add_months(date(today.year, today.month, 1), -1)
    prev = add_months(last, -1)
    return last.strftime("%B %Y"), prev.strftime("%B %Y")


def last_complete_periods(today: date) -> list[str]:
    """The last complete month and quarter, written as a question names them."""
    return [_last_complete("month", today)[0], _last_complete("quarter", today)[0]]


def _calendar_unit(window: Window) -> str | None:
    days = (window.end - window.start).days
    if days == 1:
        return "day"
    if days == 7 and window.start.weekday() == 0:
        return "week"
    if not window.month_aligned():
        return None
    months = window.months()
    start = window.start.month
    if months == 1:
        return "month"
    if months == 3 and start % 3 == 1:
        return "quarter"
    if months == 6 and start in (1, 7):
        return "half"
    if months == 12 and start == 1:
        return "year"
    return None


def _shifted(window: Window, unit: str) -> str:
    """The window one ``unit`` earlier, as compare's explicit range."""
    if unit in _MONTHS_IN:
        # The exclusive end moves, so a month's last day stays its last day.
        months = _MONTHS_IN[unit]
        a = add_months(window.start, -months)
        b = add_months(window.end, -months) - timedelta(days=1)
    else:
        step = timedelta(days=7 if unit == "week" else 1)
        a, b = window.start - step, window.end - step - timedelta(days=1)
    return f"{a.isoformat()}/{b.isoformat()}"


def target(
    reading: Reading, window: Window, to_date: str | None, grain: str | None
) -> tuple[str, str | None] | ClarificationRequest:
    """``(compare.to, grain)`` for the answer's own ``window``. ``to_date``:
    the unit of a calendar period read to date ("this month"), compared with
    the same days of the period before it."""
    if reading.against is not None:
        a, b = reading.against
        return f"{a.isoformat()}/{b.isoformat()}", grain
    unit = reading.unit
    if unit == "year":
        return "previous_year", grain
    calendar = to_date or _calendar_unit(window)
    if unit is not None and unit != calendar:
        if calendar is not None and _ORDER.index(calendar) < _ORDER.index(unit):
            return ClarificationRequest(
                kind="unresolved_slot",
                term="window",
                question=f"The question asks for a {unit}-on-{unit} change over a window "
                f"shorter than a {unit}. Which {unit} should be compared with the one before?",
                options=[_last_complete(unit, window.start)[0]],
            )
        if grain not in (None, unit):
            return ClarificationRequest(
                kind="unresolved_slot",
                term="grain",
                question=f"The question asks for a {unit}-on-{unit} change broken down by "
                f"{grain}. Should each {unit} be compared with the one before it?",
                options=[f"by {unit}"],
            )
        # A window of several units: each unit against the one before it.
        return _shifted(window, unit), unit
    if to_date is not None:
        return _shifted(window, to_date), grain
    return "previous_period", grain


def window_of(filters: list[IntentFilter], field: str) -> Window | None:
    """The half-open window the intent's filters bound on ``field``, or None."""
    on = [f for f in filters if f.field == field]
    if not on:
        return None
    try:
        return intent_window(on, field)
    except CompileError:
        return None


# ── a ratio needs a period ──────────────────────────────────────────────────

# A question asking over the whole history says so.
_ALL_TIME = re.compile(
    r"\b(?:all[- ]time|ever|lifetime|historically|since\s+(?:the\s+)?"
    r"(?:start|beginning|launch|inception))\b",
    re.I,
)
# A filter on a column named for a period ("month", "is_current_week") is a window.
_PERIOD_WORDS = frozenset(
    "day week month quarter year date period today latest current mtd qtd ytd wtd".split()
)


def is_ratio(metric: Metric) -> bool:
    """A ratio, a derived division, or a metric formatted as a percent."""
    return (
        metric.type == "ratio"
        or metric.format == "percent"
        or (metric.type == "derived" and "/" in (metric.expr or ""))
    )


# The ledger's provider for a period dst pinned because the question named
# none: a declared default, not a decision anyone measured.
PERIOD_DEFAULT = "last-complete-period"


def pinned_window(month: tuple[date, date]) -> DecisionRecord:
    """The ledger record of a ratio pinned to the last complete month."""
    a, b = month
    return DecisionRecord(
        slot="window",
        chosen=f"{a.isoformat()}/{b.isoformat()}",
        verdict="act",
        provider=PERIOD_DEFAULT,
    )


def last_complete_month(today: date) -> tuple[date, date]:
    first = add_months(date(today.year, today.month, 1), -1)
    return first, date(today.year, today.month, 1) - timedelta(days=1)


def pin_disclosure(records: list[DecisionRecord], metrics: list[str]) -> str | None:
    """The line an answer carries when its period was pinned, not asked."""
    pin = next((r for r in records if r.provider == PERIOD_DEFAULT and r.chosen), None)
    if pin is None or pin.chosen is None:
        return None
    a, _sep, b = pin.chosen.partition("/")
    month = date.fromisoformat(a).strftime("%B %Y")
    names = ", ".join(f"`{m.rsplit('.', 1)[-1]}`" for m in metrics)
    return (
        f"Period applied: the question names no period, so {names} is read over the "
        f"last complete month, {month} ({a} to {b}), not blended over every period. "
        "Name a period for another, or say 'all time' for the whole history."
    )


def windowless_ratio(
    question: str,
    entity: Entity,
    metrics: list[str],
    filters: list[IntentFilter],
    grain: str | None,
    period_field: str | None,
) -> str | None:
    """A ratio asked over no period of an entity that has one: over the whole
    table it blends every period into a figure nobody asked for. The ratio's
    name, or None when a window (a filter on a time field or a period-named
    column), a breakdown over time or an all-time word places it."""
    if grain or _ALL_TIME.search(question):
        return None
    by_name = {m.name: m for m in entity.metrics}
    chosen = [by_name[n] for n in (x.rsplit(".", 1)[-1] for x in metrics) if n in by_name]
    # A ratio over levels reads the latest snapshot on its own (compiler).
    ratios = [
        m.name
        for m in chosen
        if (is_ratio(m) or m.type == "derived") and not reads_as_level(m, entity)
    ]
    if not ratios:
        return None
    clocks = {
        c.lower()
        for c in (entity.default_time_field, period_field, *(m.agg_time_field for m in chosen))
        if c
    }
    if not clocks:
        return None
    dated = {f.name.lower() for f in entity.fields if f.type in ("date", "timestamp")}
    for f in filters:
        column = f.field.rsplit(".", 1)[-1].lower()
        if column in clocks | dated or set(_WORD.findall(column)) & _PERIOD_WORDS:
            return None
    return ratios[0]


def ratio_window(ratio: str, today: date) -> ClarificationRequest:
    """Ask for the period a ratio is read over, offering the last complete
    month and quarter, or the whole history said in words."""
    month, quarter = last_complete_periods(today)
    return ClarificationRequest(
        kind="unresolved_slot",
        term="window",
        question=f"'{ratio}' is a ratio, and the question names no period: over the whole "
        "table it blends every period into one figure. Which period? The last complete "
        f"month is {month}, the last complete quarter {quarter}; say 'all time' for the "
        "whole history.",
        options=[month, quarter, "all time"],
    )
