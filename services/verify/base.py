"""What every check reads and returns.

A check is a pure function of the ask (the question, the lens's view of the
model, the caller's groups, the word ledger) and the Cube queries a lane
proposed; the few that must look at data (a probe without a filter, the
readings of an ambiguous term, the rows past a limit) do it through the
``run`` the ask carries, which executes a Cube query the way serving does. A
check returns one ``Check``: pass, skip (with the reason it could not run),
disclose (served, with a note), clarify or refuse, each naming its failure
mode, and the queries rewritten when the check rewrote them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from services.contracts.cube_query import Binding, CubeFilter, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.contracts.verification import VerificationCheck
from services.cube.lens_view import CubeLensView

Rows = list[dict[str, Any]]
Runner = Callable[[CubeQuery], Rows]
Outcome = Literal["pass", "skip", "disclose", "clarify", "refuse"]
STOPS: tuple[Outcome, ...] = ("clarify", "refuse")


@dataclass
class Ask:
    question: str
    lens: CubeLensView
    config: LensConfig | None = None
    groups: list[str] = field(default_factory=list)
    # The lane's word ledger; None when the lane wrote none (the word check
    # then fails loud instead of passing on nothing).
    ledger: list[Binding] | None = None
    # The queries as the lane wrote them: the word and scope checks judge
    # these, never a reading another check substituted.
    asked: list[CubeQuery] = field(default_factory=list)
    run: Runner | None = None
    # The lens clock's today (the business day in the lens timezone): the
    # day the question's relative periods resolve against.
    today: date | None = None
    # Driver mode: the question text is the agent's, so a reading's select
    # words are read from the question the route recorded for the request
    # (``recorded``), never from ``question``. None recorded: no select word
    # applies and the default reading stands, the others disclosed.
    driver: bool = False
    recorded: str | None = None

    @property
    def clock_end(self) -> str | None:
        return self.lens.probe.clock_end

    @property
    def select_text(self) -> str:
        """The text an ambiguous term's select words are read from: the
        question on the lanes that read it themselves, the recorded route
        question in driver mode."""
        if self.driver:
            return self.recorded or ""
        return self.question


@dataclass
class Check:
    name: str
    mode: FailureMode | None
    outcome: Outcome = "pass"
    detail: str = ""
    queries: list[CubeQuery] | None = None
    rows: Rows | None = None

    @property
    def stops(self) -> bool:
        return self.outcome in STOPS

    def verification(self) -> VerificationCheck:
        status: Literal["pass", "fail", "skip"] = (
            "skip" if self.outcome == "skip" else "fail" if self.stops else "pass"
        )
        return VerificationCheck(name=self.name, status=status, reason=self.detail or None)


def passed(name: str, mode: FailureMode | None, detail: str = "") -> Check:
    return Check(name, mode, "pass", detail)


# ── reading a query ─────────────────────────────────────────────────────────


def flat_filters(q: CubeQuery) -> list[CubeFilter]:
    """Member clauses, a top-level ``and`` flattened; an ``or`` group's
    clauses are not a restriction on their own and are left out."""
    out: list[CubeFilter] = []
    for f in q.filters:
        if f.and_ is not None:
            out += [x for x in f.and_ if x.member]
        elif f.member:
            out.append(f)
    return out


def all_filters(q: CubeQuery) -> list[CubeFilter]:
    out: list[CubeFilter] = []
    stack = list(q.filters)
    while stack:
        f = stack.pop(0)
        if f.member:
            out.append(f)
        stack += list(f.and_ or []) + list(f.or_ or [])
    return out


def equals_values(q: CubeQuery, lens: CubeLensView, member: str) -> set[str] | None:
    """The values an equals filter on ``member`` (directly or through a view)
    pins, or None when nothing pins it."""
    values: set[str] | None = None
    for f in q.filters:
        if f.member and lens.base(f.member) == member and f.operator == "equals":
            values = set(f.values or [])
    return values


def used(q: CubeQuery, lens: CubeLensView) -> tuple[set[str], list[str], set[str], bool]:
    """``(members, values, segments, windowed)``: every member the query uses
    (as written and its base), the filter values and the values of every
    dimension it breaks down by, its segments, and whether it reads a window."""
    members: set[str] = set()
    for m in (*q.measures, *q.dimensions, *q.segments, *(o for o, _ in q.order)):
        members |= {m, lens.base(m)}
    values: list[str] = []
    windowed = False
    for f in all_filters(q):
        assert f.member is not None
        members |= {f.member, lens.base(f.member)}
        values += list(f.values or [])
        if lens.is_time(f.member):
            windowed = True
    for td in q.timeDimensions:
        members |= {td.dimension, lens.base(td.dimension)}
        windowed = windowed or bool(td.dateRange or td.compareDateRange)
    for d in q.dimensions:
        values += lens.dictionary(d) or []
    segments = set(q.segments) | {lens.base(s) for s in q.segments}
    return members, values, segments, windowed


def windows(q: CubeQuery, lens: CubeLensView) -> list[tuple[str | None, str]]:
    """``[(start | None, end)]`` the event windows the query reads (a
    ``dateRange`` or each ``compareDateRange``); a schedule date (due, end of
    contract) is not a window. None read = as of the lens clock."""
    out: list[tuple[str | None, str]] = []
    for td in q.timeDimensions:
        facts = lens.time_facts(td.dimension)
        if facts is not None and facts.kind == "schedule":
            continue
        ranges: list[Any] = []
        if td.compareDateRange:
            ranges = list(td.compareDateRange)
        elif isinstance(td.dateRange, list):
            ranges = [td.dateRange]
        out += [(str(r[0])[:10], str(r[-1])[:10]) for r in ranges if isinstance(r, list)]
    return out or [(None, lens.probe.clock_end or "9999-12-31")]


def fnum(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def copy(q: CubeQuery) -> CubeQuery:
    return q.model_copy(deep=True)
