"""The figure a question asks for when the lane answered with its parts.

A share, a ratio or a per-day average is one number; the lane composes it
as two Cube queries (numerator and denominator) or one total over a window,
and a caller then has to divide, or divide by the days in a window the
answer never states. dst owns the arithmetic: it is deterministic over the
parts, and the window it counts is the one that ran. The figure leads the
answer with the parts beside it; when the parts cannot be divided (they
read different windows, or more than one measure each), nothing is divided
and the answer says why. Nothing here is prose judgment: which word asks
for which arithmetic is a fixed small vocabulary, and anything else is left
as parts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from services.contracts.cube_query import CubeQuery
from services.cube.lens_view import CubeLensView, norm
from services.verify.base import Rows, all_filters, fnum, windows

SHARE = re.compile(r"\b(share|percentage|percent|proportion|fraction)\b|%")
PER_UNIT = {
    "per day": re.compile(r"\b(per day|a day|daily average|average daily|average per day)\b"),
    "per week": re.compile(r"\b(per week|a week|weekly average|average weekly|average per week)\b"),
}
RATIO = re.compile(r"\b(ratio|per|divided by|relative to)\b")
DAYS = {"per day": 1, "per week": 7}


@dataclass
class Derived:
    """``kind`` is the figure asked (``share``, ``ratio``, ``per day``,
    ``per week``); ``rows`` carry it per group of the numerator (one row
    when both parts are scalar), under the column ``kind``; ``note`` says
    what was divided by what, or why nothing was. ``computed`` is False when
    the parts could not be divided."""

    kind: str
    rows: Rows = field(default_factory=list)
    note: str = ""
    computed: bool = True

    def lead(self) -> str:
        """The first sentence of the answer."""
        if not self.computed:
            return f"{self.kind}: not computed - {self.note}"
        shown = []
        for r in self.rows:
            keys = [f"{v}" for k, v in r.items() if k != self.kind]
            value = r[self.kind]
            figure = (
                "n/a"
                if value is None
                else f"{value:.1%}"
                if self.kind == "share"
                else f"{value:,.2f}"
            )
            shown.append(f"{', '.join(keys)}: {figure}" if keys else figure)
        return f"{self.kind}: {'; '.join(shown)} ({self.note})"


def asked(question: str) -> str | None:
    """Which derived figure the question asks for, if any: the vocabulary is
    fixed, a share before a per-unit average before a plain ratio."""
    q = norm(question)
    if SHARE.search(q):
        return "share"
    for kind, rx in PER_UNIT.items():
        if rx.search(q):
            return kind
    if RATIO.search(q):
        return "ratio"
    return None


def _keys(rows: Rows, measure: str) -> list[str]:
    cols: list[str] = []
    for r in rows:
        cols += [c for c in r if c != measure and c not in cols]
    return cols


def _filter_set(q: CubeQuery) -> set[tuple[str, str, tuple[str, ...]]]:
    return {
        (f.member or "", f.operator or "", tuple(f.values or []))
        for f in all_filters(q)
        if f.member and not any(f.member == t.dimension for t in q.timeDimensions)
    } | {("segment", s, ()) for s in q.segments}


def _narrower(a: CubeQuery, b: CubeQuery) -> bool:
    """``a`` reads a strict subset of ``b``'s rows by its filters: the
    numerator of a share."""
    fa, fb = _filter_set(a), _filter_set(b)
    return fa > fb


def _window_label(q: CubeQuery, lens: CubeLensView) -> str:
    return ", ".join(f"{s or 'start'}..{e}" for s, e in windows(q, lens))


def derive(
    question: str, parts: list[tuple[CubeQuery, Rows]], lens: CubeLensView
) -> Derived | None:
    """The asked figure from the served parts, or None when the question
    asks for none, or the parts are not the shape the figure needs (one
    measure each; two parts for a share or ratio, one windowed part for a
    per-unit average)."""
    kind = asked(question)
    if kind is None:
        return None
    if kind in DAYS:
        return _per_unit(kind, parts, lens)
    if len(parts) != 2 or any(len(q.measures) != 1 for q, _ in parts):
        return None
    (q1, r1), (q2, r2) = parts
    if kind == "share" and _narrower(q2, q1):
        (q1, r1), (q2, r2) = (q2, r2), (q1, r1)
    w1, w2 = windows(q1, lens), windows(q2, lens)
    if w1 != w2:
        return Derived(
            kind,
            computed=False,
            note=(
                f"the parts read different windows ({q1.measures[0]} over "
                f"{_window_label(q1, lens)}, {q2.measures[0]} over {_window_label(q2, lens)}); "
                "say which window the figure should read and both parts will be read over it"
            ),
        )
    m1, m2 = q1.measures[0], q2.measures[0]
    k1, k2 = _keys(r1, m1), _keys(r2, m2)
    if k2 and k2 != k1:
        return Derived(
            kind,
            computed=False,
            note=f"the denominator is broken down by {', '.join(k2)} and the numerator by "
            f"{', '.join(k1) or 'nothing'}; the parts are shown as served",
        )
    den: dict[tuple[Any, ...], Any] = {tuple(r.get(k) for k in k2): r.get(m2) for r in r2}
    if not k2 and len(r2) != 1:
        return None
    out: Rows = []
    for r in r1 or [{m1: None}]:
        d = den.get(tuple(r.get(k) for k in k2) if k2 else ())
        n = r.get(m1)
        value = None if n is None or d in (None, "") or fnum(d) == 0 else fnum(n) / fnum(d)
        out.append({**{k: r.get(k) for k in k1}, kind: value})
    windowed = any(t.dateRange or t.compareDateRange for t in q1.timeDimensions)
    how = f"{m1} / {m2}" + (f", over {_window_label(q1, lens)}" if windowed else "")
    if not k2 and len(r1) == 1:
        how = f"{r1[0].get(m1)} / {r2[0].get(m2)}: {how}"
    return Derived(kind, out, how)


def _per_unit(kind: str, parts: list[tuple[CubeQuery, Rows]], lens: CubeLensView) -> Derived | None:
    if len(parts) != 1 or len(parts[0][0].measures) != 1:
        return None
    q, rows = parts[0]
    m = q.measures[0]
    rng = next((t.dateRange for t in q.timeDimensions if isinstance(t.dateRange, list)), None)
    if not rng:
        return Derived(
            kind,
            computed=False,
            note=f"a {kind} average needs a window to count; the query reads none",
        )
    start, end = date.fromisoformat(str(rng[0])[:10]), date.fromisoformat(str(rng[-1])[:10])
    days = (end - start).days + 1
    units = days / DAYS[kind]
    keys = _keys(rows, m)
    out: Rows = []
    for r in rows or [{m: None}]:
        total = r.get(m)
        value = None if total is None or units <= 0 else fnum(total) / units
        out.append({**{k: r.get(k) for k in keys}, kind: value})
    span = f"{days} days" if kind == "per day" else f"{units:g} weeks ({days} days)"
    total_text = f"{rows[0].get(m)} / " if rows and not keys else f"{m} / "
    return Derived(kind, out, f"{total_text}{span}, {start.isoformat()}..{end.isoformat()}")
