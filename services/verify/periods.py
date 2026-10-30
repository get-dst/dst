"""The question's relative periods, resolved by dst before any model reads it.

"This week" asked on Tuesday 2026-09-01 is 2026-08-31..2026-09-06 whatever a
model believes the date is. The phrases resolve with ``timewindow`` on the
lens clock (the business day in the lens's timezone), ISO weeks starting on
Monday: the lane hands them to the model as facts, and the window check binds
the query to them.

Only a question whose every period is a plain calendar phrase of one grain
(this / last / next day, week, month, quarter, year; today, yesterday, the
to-date shorthands) resolves. A phrase that modifies another period ("the
same month last year", "August this year", "since last week") or a rolling
one ("the past week", "this time last year") is left to the query as written: binding it would be a
guess, and a guard here must never fire on a right window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from services.runtime.timewindow import bare_months, temporal_terms, window_ranges

_CALENDAR = re.compile(r"(this|last|next) (day|week|month|quarter|year)")
_MODIFIED = re.compile(
    r"\b(?:same|ago|since|before|after|until|till|prior to|earlier|later|past|trailing|"
    r"rolling|this time|right now)\b",
    re.IGNORECASE,
)
_LABEL = {"this day": "today", "last day": "yesterday"}


@dataclass(frozen=True)
class Period:
    phrase: str
    start: date
    end: date
    current: bool  # today falls inside it: the period is not over yet

    @property
    def window(self) -> list[str]:
        return [self.start.isoformat(), self.end.isoformat()]

    def label(self) -> str:
        span = f"{self.start.isoformat()}..{self.end.isoformat()}"
        week = " (ISO week, Monday to Sunday)" if self.phrase.endswith("week") else ""
        return f"'{self.phrase}' is {span}{week}"


def resolve(question: str, today: date | None) -> list[Period]:
    """The question's calendar periods, earliest first; [] when it states
    none, or one this module does not bind (see the module docstring)."""
    if today is None or _MODIFIED.search(question) or bare_months(question):
        return []
    terms = temporal_terms(question)
    if not terms or not all(_CALENDAR.fullmatch(t) for t in terms):
        return []
    if len({t.split()[1] for t in terms}) != 1:
        return []
    out: list[Period] = []
    for t in terms:
        (span,) = window_ranges(frozenset({t}), today)
        start, end = span
        out.append(Period(_LABEL.get(t, t), start, end, start <= today <= end))
    return sorted(out, key=lambda p: p.start)


def facts(periods: list[Period], today: date, loaded: str | None) -> str:
    """The periods as the model's user turn states them."""
    if not periods:
        return ""
    lines = []
    for p in periods:
        line = p.label()
        if p.current:
            line += f"; it is not over (today is {today.isoformat()}"
            line += f", data loaded through {loaded})" if loaded else ")"
        lines.append(f"- {line}")
    return (
        "PERIODS (resolved by dst from the question; use exactly these dates as the "
        "dateRange of each):\n" + "\n".join(lines)
    )
