"""Deterministic time-window extraction — shared by the certified matcher's
temporal veto and the `window_applied` verification check.

"this quarter" and "last quarter" are one token apart and embed near-identical,
but the qualifier IS the question. Embeddings are never asked to carry this
distinction, and neither is a model's sense of what day it is: extraction is a
small closed vocabulary, normalized, compared as sets — false positives are
forbidden by construction, so everything unrecognized is simply not a
window."""

from __future__ import annotations

import re
from datetime import date, timedelta

_REL_NORM = {"current": "this", "previous": "last", "prior": "last", "past": "last"}
_MTD = {"ytd": "this year", "qtd": "this quarter", "mtd": "this month"}
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
# Abbreviations count only beside a year or inside a span ("Aug 2026",
# "Jan–Aug 2026"): bare "mar", "dec", "jan" are words and names, so on their
# own they are not a window and are never flagged as an unplaced month.
_ABBR = {
    "jan": "january",
    "feb": "february",
    "mar": "march",
    "apr": "april",
    "jun": "june",
    "jul": "july",
    "aug": "august",
    "sept": "september",
    "sep": "september",
    "oct": "october",
    "nov": "november",
    "dec": "december",
}
_MONTH_ANY = _MONTHS + "|sept|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
_TEMPORAL = re.compile(
    r"\b(?:"
    r"(?P<rel>this|current|last|previous|prior|next|past|trailing)\s+"
    r"(?:(?P<n>\d+)\s+)?(?P<grain>day|week|month|quarter|year)s?"
    r"|(?P<word>today|yesterday|ytd|qtd|mtd)"
    r"|(?<![\w-])q(?P<qn>[1-4])(?:\s*(?:of\s*)?(?P<qyear>(?:19|20)\d{2}))?"
    rf"|(?P<month>{_MONTH_ANY})\.?\s+(?P<myear>(?:19|20)\d{{2}})"
    # ISO-shaped periods as a binding or a person writes them: 2026-08-31,
    # 2026-08, 2026-Q2. Before the bare year, or the year alone matches and the
    # day or month is lost ("as of 2026-08-31" read as the whole of 2026).
    r"|(?P<iday>(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?![\w-])"
    r"|(?P<iyear>(?:19|20)\d{2})-(?:(?P<imonth>0[1-9]|1[0-2])|q(?P<iq>[1-4]))(?![\w-])"
    r"|(?P<year>(?:19|20)\d{2})"
    r")\b",
    re.IGNORECASE,
)


def _month_name(raw: str) -> str:
    return _ABBR.get(raw.lower(), raw.lower())


# "January through August 2026", "March to June 2026", "Apr–Jun 2026",
# "from January to September 2026 inclusive", "between March and June 2026":
# one span, first month's start to last month's END whatever the connector
# word — "to" and "until" bind the last month whole, exactly like "through".
_SPAN = re.compile(
    rf"\b(?:from\s+|(?P<between>between)\s+)?(?P<m1>{_MONTH_ANY})\.?\s*"
    rf"(?:through|thru|to|until|till|up to|-|–|—|(?(between)and|(?!)))\s*"
    rf"(?P<m2>{_MONTH_ANY})\.?(?:\s+(?:inclusive|incl\.?))?\s+(?P<syear>(?:19|20)\d{{2}})"
    rf"(?:\s+(?:inclusive|incl\.?))?\b",
    re.IGNORECASE,
)
_MONTH_WORD = re.compile(rf"\b(?P<month>{_MONTHS})\b", re.IGNORECASE)


def temporal_terms(text: str) -> frozenset[str]:
    """The normalized time-window terms a question states. Deliberately small:
    relative windows (this/last/next × grain, incl. 'last 30 days'), the *td
    shorthands, explicit quarters/months with years, month spans with a year
    ("January through August 2026" → one span term), and bare years. A month
    name alone is NOT a window ('may' is a modal verb)."""
    out: set[str] = set()
    spanned: list[tuple[int, int]] = []
    for s in _SPAN.finditer(text):
        out.add(f"span:{_month_name(s.group('m1'))}-{_month_name(s.group('m2'))}")
        out.add(s.group("syear"))
        spanned.append(s.span())
    for m in _TEMPORAL.finditer(text):
        if any(a <= m.start() < b for a, b in spanned):
            continue  # the span already carries this month and year
        if rel := m.group("rel"):
            rel = _REL_NORM.get(rel.lower(), rel.lower())
            rel = "last" if rel == "trailing" else rel
            n = f"{m.group('n')} " if m.group("n") else ""
            out.add(f"{rel} {n}{m.group('grain').lower()}")
        elif word := m.group("word"):
            word = word.lower()
            out.add({"today": "this day", "yesterday": "last day"}.get(word) or _MTD[word])
        elif qn := m.group("qn"):
            out.add(f"q{qn}")
            if m.group("qyear"):
                out.add(m.group("qyear"))
        elif month := m.group("month"):
            out.add(_month_name(month))
            out.add(m.group("myear"))
        elif iday := m.group("iday"):
            out.add(iday)
        elif iyear := m.group("iyear"):
            out.add(iyear)
            if iq := m.group("iq"):
                out.add(f"q{iq}")
            else:
                out.add(_MONTH_BY_NUM[int(m.group("imonth"))])
        elif year := m.group("year"):
            out.add(year)
    return frozenset(out)


# A breakdown phrase: the question wants one figure PER calendar bucket.
# "by month", "per week", "each quarter", "end of every month", "a monthly
# trend". A bare "monthly" or "weekly" is not one: it names metrics ("monthly
# recurring revenue", "weekly active users") far more often than a series.
_BREAKDOWN = re.compile(
    r"\b(?:by|per|each|every|for\s+each|of\s+each|of\s+every)\s+"
    r"(?P<unit>week|month|quarter|year)s?\b"
    r"|\b(?P<adv>weekly|monthly|quarterly|yearly|annual)\s+"
    r"(?:trend|breakdown|series|basis|progression|development|view)\b",
    re.IGNORECASE,
)
_ADVERB_UNIT = {"weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year"}


def breakdown_unit(text: str) -> str | None:
    """The calendar unit a question asks a breakdown by (week, month, quarter,
    year), or None when it asks for one figure. Deterministic: the phrase
    alone, no reading of the rest of the sentence."""
    m = _BREAKDOWN.search(text)
    if m is None:
        return None
    if m.group("unit"):
        return m.group("unit").lower()
    return _ADVERB_UNIT.get(m.group("adv").lower(), "year")


def temporal_spans(text: str) -> list[tuple[int, int]]:
    """The character spans of every window phrase ``temporal_terms`` reads off
    the text, so a caller can blank them and judge what the question says
    besides its period."""
    spans = [m.span() for m in _SPAN.finditer(text)]
    for m in _TEMPORAL.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return sorted(spans)


def bare_months(text: str) -> list[tuple[int, int]]:
    """The spans of month names the text states without a year or a span —
    "in September" — which ``temporal_terms`` does not read as a window. A
    bare "may" is the modal verb and is never one."""
    covered = temporal_spans(text)
    return [
        m.span()
        for m in _MONTH_WORD.finditer(text)
        if m.group("month").lower() != "may" and not any(a <= m.start() < b for a, b in covered)
    ]


def place_bare_month(text: str, today: date) -> str:
    """The text with its one bare month name given the year of its most recent
    occurrence on or before ``today``'s month — "in September" asked in
    October 2026 is September 2026; asked in March 2026 it is September 2025.
    Unchanged when the text states no bare month, or more than one."""
    spans = bare_months(text)
    if len(spans) != 1:
        return text
    a, b = spans[0]
    month = _MONTH_NUM[text[a:b].lower()]
    year = today.year if month <= today.month else today.year - 1
    return f"{text[:b]} {year}{text[b:]}"


def unplaced_months(text: str, terms: frozenset[str]) -> list[str]:
    """Month names the question states that no term carries — "January and
    August 2026" parses August 2026 and drops January; served as August alone
    that is a silently wrong period, so the caller of the window must ask.
    A bare "may" is left alone (the modal verb)."""
    covered: set[str] = set()
    for t in terms:
        if t in _MONTH_NUM:
            covered.add(t)
        elif t.startswith("span:"):
            a, b = t[5:].split("-", 1)
            if a in _MONTH_NUM and b in _MONTH_NUM and _MONTH_NUM[a] <= _MONTH_NUM[b]:
                covered.update(
                    n for n, i in _MONTH_NUM.items() if _MONTH_NUM[a] <= i <= _MONTH_NUM[b]
                )
    out: list[str] = []
    for m in _MONTH_WORD.finditer(text):
        name = m.group("month").lower()
        if name == "may" or name in covered or name in out:
            continue
        out.append(name)
    return out


def temporal_mismatch(asked: str, approved: str) -> bool:
    """True only when both questions state a window and the windows differ."""
    a, b = temporal_terms(asked), temporal_terms(approved)
    return bool(a) and bool(b) and a != b


# ── window → calendar dates ──────────────────────────────────────────────────
# `window_ranges` turns the extracted terms into concrete spans so the
# date-coverage check can compare them against a table's measured MIN/MAX.
# Same discipline as extraction: only the unambiguous subset resolves — a bare
# "q3" with no year, or a term outside the vocabulary, yields nothing rather
# than a guess, because everything downstream treats "entirely outside
# coverage" as grounds to speak up.

_MONTH_NUM = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_BY_NUM = {i: n for n, i in _MONTH_NUM.items()}

_REL_TERM = re.compile(r"(this|last|next) (?:(\d+) )?(day|week|month|quarter|year)")


def _month_end(y: int, m: int) -> date:
    return date(y, 12, 31) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)


def _month_first(anchor: date, months_offset: int) -> date:
    total = anchor.year * 12 + (anchor.month - 1) + months_offset
    y, m0 = divmod(total, 12)
    return date(y, m0 + 1, 1)


def _calendar_period(today: date, rel: str, grain: str) -> tuple[date, date]:
    offset = {"this": 0, "last": -1, "next": 1}[rel]
    if grain == "day":
        d = today + timedelta(days=offset)
        return d, d
    if grain == "week":
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return monday, monday + timedelta(days=6)
    if grain == "month":
        start = _month_first(today, offset)
        return start, _month_end(start.year, start.month)
    if grain == "quarter":
        anchor = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        start = _month_first(anchor, 3 * offset)
        last = _month_first(start, 2)
        return start, _month_end(last.year, last.month)
    y = today.year + offset
    return date(y, 1, 1), date(y, 12, 31)


def window_ranges(terms: frozenset[str], today: date) -> list[tuple[date, date]]:
    """The concrete [start, end] spans the stated windows resolve to — [] when
    none do. Relative windows resolve against ``today`` (the lens clock);
    explicit quarters/months pair with every stated year ("Q3 2025 vs Q3 2026"
    yields both), and a bare year stands alone only when no sub-year period was
    stated. "last 3 months" is the trailing window ending today. Only the
    unambiguous subset resolves — false positives downstream are forbidden, so
    an unresolvable window is simply not a range."""
    ranges: list[tuple[date, date]] = []
    months = sorted(_MONTH_NUM[t] for t in terms if t in _MONTH_NUM)
    span_terms = [t for t in terms if t.startswith("span:")]
    spans: list[tuple[int, int]] = []
    for t in span_terms:
        first, last = t[5:].split("-", 1)
        if first in _MONTH_NUM and last in _MONTH_NUM and _MONTH_NUM[first] <= _MONTH_NUM[last]:
            spans.append((_MONTH_NUM[first], _MONTH_NUM[last]))
        # a backwards span ("August through January 2026") resolves to nothing
    quarters = sorted(int(t[1]) for t in terms if re.fullmatch(r"q[1-4]", t))
    years = sorted(int(t) for t in terms if re.fullmatch(r"(?:19|20)\d{2}", t))
    for t in sorted(t for t in terms if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)):
        try:
            day = date.fromisoformat(t)
        except ValueError:
            continue  # 2026-02-30 is not a day; unresolved, never a guess
        ranges.append((day, day))
    for y in years:
        for m in months:
            ranges.append((date(y, m, 1), _month_end(y, m)))
        for m_from, m_to in spans:
            ranges.append((date(y, m_from, 1), _month_end(y, m_to)))
        for q in quarters:
            ranges.append((date(y, 3 * (q - 1) + 1, 1), _month_end(y, 3 * q)))
        # A bare year stands alone only when no sub-year period was stated —
        # a span that did not resolve (backwards) still WAS stated.
        if not months and not quarters and not span_terms:
            ranges.append((date(y, 1, 1), date(y, 12, 31)))
    for t in terms:
        rel_match = _REL_TERM.fullmatch(t)
        if rel_match is None:
            continue
        rel, n, grain = rel_match.group(1), rel_match.group(2), rel_match.group(3)
        if n is None:
            ranges.append(_calendar_period(today, rel, grain))
            continue
        if rel not in ("last", "next"):
            continue  # "this 3 months" is not a window anyone states — unresolved
        if grain in ("day", "week"):
            span = timedelta(days=int(n)) if grain == "day" else timedelta(weeks=int(n))
        else:
            months_per = {"month": 1, "quarter": 3, "year": 12}[grain]
            # Day-of-month clamped to 28: up to three days of slack, always
            # WIDENING the window — a wider trailing window can only make
            # "entirely outside coverage" rarer, never invent it.
            edge = _month_first(today, months_per * int(n) * (1 if rel == "next" else -1))
            span = abs(today - edge.replace(day=min(today.day, 28)))
        if rel == "last":
            ranges.append((today - span, today))
        else:
            ranges.append((today, today + span))
    return ranges


# Date-shaped evidence in SQL: any of these means the query engaged a time
# window at all — the deliberately COARSE half of window_applied.
# The check never grades which window (that needs full date arithmetic); it
# only catches the blatant failure: a windowed question whose SQL carries NO
# date handling anywhere, served as a period figure.
_SQL_DATE_EVIDENCE = re.compile(
    r"CURRENT_DATE|CURRENT_TIMESTAMP|GETDATE|NOW\s*\(|SYSDATE"
    r"|DATE_TRUNC|DATETRUNC|DATE_SUB|DATE_ADD|DATEADD|DATEDIFF|DATE_DIFF"
    r"|EXTRACT\s*\(|LAST_DAY|INTERVAL\s"
    r"|'(?:19|20)\d{2}-\d{2}(?:-\d{2})?"  # ISO date/month literal
    r"|(?:19|20)\d{2}\s*[-)]|[=<>]\s*(?:19|20)\d{2}\b",  # bare year comparisons
    re.IGNORECASE,
)


def sql_engages_time(sql: str, time_fields: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether the SQL shows ANY date handling: a date function, a date/year
    literal, or a reference to a declared time field. Coarse on purpose —
    the FALSE branch must be unambiguous, because it caps a badge."""
    if _SQL_DATE_EVIDENCE.search(sql):
        return True
    lowered = sql.lower()
    return any(re.search(rf"\b{re.escape(f.lower())}\b", lowered) for f in time_fields if f)
