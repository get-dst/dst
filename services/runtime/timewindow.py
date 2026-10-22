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
    r"|(?<![\w-])h(?P<hn>[12])(?:\s*(?:of\s*)?(?P<hyear>(?:19|20)\d{2}))?"
    rf"|(?P<month>{_MONTH_ANY})\.?\s+(?P<myear>(?:19|20)\d{{2}})"
    # ISO-shaped periods as a binding or a person writes them: 2026-08-31,
    # 2026-08, 2026-Q2. Before the bare year, or the year alone matches and the
    # day or month is lost ("as of 2026-08-31" read as the whole of 2026).
    r"|(?P<iday>(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?![\w-])"
    r"|(?P<iyear>(?:19|20)\d{2})-(?:(?P<imonth>0[1-9]|1[0-2])|q(?P<iq>[1-4])|h(?P<ih>[12]))(?![\w-])"
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

# ── days written in words ────────────────────────────────────────────────────
# "on 28 August 2026", "28 Aug 2026", "August 28, 2026", "between 5 and 12
# October", "from 5 to 12 October 2026", "5–12 Oct", "October 5 to 12", "the
# week of 5 October", "the first half of August 2026", and ISO ranges "from
# 2026-03-20 to 2026-03-28". Read before the month patterns: "28 August 2026"
# read as "August 2026" answers the whole month for a one-day question.
# A day's term is its ISO date (``2026-08-28``); a span of days is
# ``<first>..<last>``. A day without a year is ``--MM-DD`` and takes the most
# recent occurrence on or before the lens clock when it resolves
# (``window_ranges``); ``end`` as the day is the month's last.
_DAY = r"(?<![\d.])(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?(?!\d)"
_MON = rf"(?:{_MONTH_ANY})\.?"
_YR = r"(?:19|20)\d{2}"
_ISO = r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
_TO = r"(?:\s*(?:to|through|thru|until|till|-|–|—)\s*|\s+(?(between)and|(?!))\s+)"
_DAY_RANGE_DM = re.compile(
    rf"\b(?:(?P<between>between)\s+|from\s+)?(?P<d1>{_DAY})(?:\s+(?:of\s+)?(?P<m1>{_MON}))?"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y1>{_YR}))?{_TO}(?:the\s+)?(?P<d2>{_DAY})(?:\s+of)?\s+(?P<m2>{_MON})"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y2>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_DAY_RANGE_MD = re.compile(
    rf"\b(?:(?P<between>between)\s+|from\s+)?(?P<m1>{_MON})\s+(?P<d1>{_DAY})"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y1>{_YR}))?{_TO}(?:(?P<m2>{_MON})\s+)?(?P<d2>{_DAY})"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y2>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_DAY_RANGE_ISO = re.compile(
    rf"\b(?:(?P<between>between)\s+|from\s+)?(?P<a>{_ISO})"
    rf"(?:\s*(?:to|through|thru|until|till|\.\.|–|—)\s*|\s+-\s+|\s+(?(between)and|(?!))\s+)"
    rf"(?P<b>{_ISO})(?![\w-])",
    re.IGNORECASE,
)
_WEEK_OF = re.compile(
    rf"\b(?:the\s+)?week\s+(?:of|starting|beginning|commencing)\s+(?:the\s+)?"
    rf"(?:(?P<iso>{_ISO})|(?P<d1>{_DAY})(?:\s+of)?\s+(?P<m1>{_MON})|(?P<m2>{_MON})\s+(?P<d2>{_DAY}))"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_HALF = re.compile(
    rf"\b(?P<half>first|second|1st|2nd)\s+half\s+of\s+(?P<m>{_MON})(?:,?\s+(?:(?:in|of)\s+)?(?P<y>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_ONE_DAY = re.compile(
    rf"\b(?:(?P<d1>{_DAY})(?:\s+of)?\s+(?P<m1>{_MON})|(?P<m2>{_MON})\s+(?P<d2>{_DAY}))"
    rf"(?:,?\s+(?:(?:in|of)\s+)?(?P<y>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fourteen": 14,
    "fifteen": 15,
}
_N = r"(?:[1-9]|[12]\d|30|31|one|two|three|four|five|six|seven|eight|nine|ten|fourteen|fifteen)"
_MONTH_TAIL = rf"(?:the\s+month\s+of\s+)?(?P<m>{_MON})(?:,?\s+(?:(?:in|of)\s+)?(?P<y>{_YR}))?"
# "the first 7 days of October", "the last 3 days of August", "the first week
# of August", "the last day of February 2026".
_FIRST_LAST = re.compile(
    rf"\b(?P<which>first|last|final|opening|closing)\s+(?:(?P<n>{_N})\s+)?"
    rf"(?P<unit>day|days|week|weeks)\s+(?:of|in)\s+{_MONTH_TAIL}(?![\w-])",
    re.IGNORECASE,
)
# "the start of August" is its first day, "the end of August 2026" its last
# ("end of 2026-08" too).
_EDGE = re.compile(
    rf"\b(?P<edge>start|beginning|end|close)\s+of\s+(?:{_MONTH_TAIL}"
    rf"|(?P<iso>(?:19|20)\d{{2}}-(?:0[1-9]|1[0-2])))(?![\w-])",
    re.IGNORECASE,
)
# Vague parts of a month: never served as the month, never guessed — the
# caller is asked, offered the days each reading would use.
_VAGUE = re.compile(
    rf"\b(?:(?P<v>early|mid|late)[\s-]+|(?:the\s+)?(?P<middle>middle)\s+of\s+){_MONTH_TAIL}"
    rf"(?![\w-])",
    re.IGNORECASE,
)
_VAGUE_DAYS = {"early": ("01", "10"), "mid": ("11", "20"), "late": ("21", "end")}
# A day-level qualifier beside a month: "first few days of August", "the
# week of August", "the 3rd week of May 2026". When the parser read none of
# these as days, the month alone is a wider window than the question asked.
_QUALIFIER = re.compile(
    r"\b(?:(?:first|last|final|opening|closing|second|third|fourth|\d{1,2}(?:st|nd|rd|th))"
    r"\s+(?:\w+\s+)?(?:days?|weeks?|fortnight)\s+(?:of|in)"
    r"|(?:early|mid|late)(?:-|\s+)"
    r"|(?:start|beginning|end|close|turn|middle)\s+of"
    r"|week\s+(?:of|starting|beginning|commencing))"
    rf"\s*(?:the\s+)?(?:month\s+of\s+)?{_MONTH_TAIL}(?![\w-])",
    re.IGNORECASE,
)
# "the start of this month", "the end of last quarter": the period's first or
# last day. With no period named, "the end of the month", "end of month",
# "month end" and "quarter-end" are the last COMPLETED period's last day (on
# 22 October, 30 September), never the current period's, which has not
# ended; "the start of the month" is the current one's first day. A period
# named elsewhere in the question ("at month end in August 2026") is that
# period's window, and "per month end" is a breakdown, not a day.
_REL_EDGE = re.compile(
    r"\b(?P<edge>start|beginning|end|close)\s+of\s+(?:(?P<rel>this|current|last|previous|"
    r"prior|next)\s+|the\s+)?(?P<grain>week|month|quarter|year)\b(?!\s+of)"
    r"|\b(?P<unit>month|quarter)[\s-]end\b",
    re.IGNORECASE,
)
_EACH_BEFORE = re.compile(r"\b(?:per|each|every|by|all)\s+(?:the\s+)?$", re.IGNORECASE)
_EDGE_TERM = re.compile(r"(start|end):(this|last|next) (week|month|quarter|year)")
# A number before a month after one of these counts things, not a day: "top
# 10 August deals", "more than 5 March signups".
_COUNTING = re.compile(
    r"\b(?:top|bottom|first|last|best|worst|highest|lowest|largest|smallest|biggest|next|"
    r"previous|over|under|above|below|than|about|around|least|most)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
# "may" is a month only where a preposition or article puts it in a date.
_MAY_DATE = re.compile(r"\b(?:on|of|from|between|since|until|till|by|before|after|the)\s+$", re.I)


def _day_num(raw: str) -> int:
    return int(re.match(r"\d+", raw).group(0))  # type: ignore[union-attr]


def _md(month: str, day: int | str) -> str:
    n = _MONTH_NUM[_month_name(month.rstrip("."))]
    return f"{n:02d}-{day:02d}" if isinstance(day, int) else f"{n:02d}-{day}"


def _dated(md: str, year: int | None) -> str:
    return f"{year}-{md}" if year is not None else f"--{md}"


def _range_term(
    m1: str, d1: int | str, m2: str, d2: int | str, y1: str | None, y2: str | None
) -> str:
    """``<first>..<last>`` for a span of days; a year stated on one end only
    carries to the other (the earlier end a year back when its month is
    later: "28 December to 3 January 2027" starts in 2026)."""
    a, b = _md(m1, d1), _md(m2, d2)
    n1, n2 = _MONTH_NUM[_month_name(m1.rstrip("."))], _MONTH_NUM[_month_name(m2.rstrip("."))]
    ya = int(y1) if y1 else None
    yb = int(y2) if y2 else None
    if ya is None and yb is not None:
        ya = yb - 1 if n1 > n2 else yb
    elif yb is None and ya is not None:
        yb = ya + 1 if n2 < n1 else ya
    return f"{_dated(a, ya)}..{_dated(b, yb)}"


def _names_a_period(text: str, span: tuple[int, int]) -> bool:
    """Does the text name a month, a year or a period outside ``span``?"""
    a, b = span
    rest = text[:a] + " " * (b - a) + text[b:]
    return bool(_MONTH_WORD.search(rest) or _TEMPORAL.search(rest))


def _day_phrases(text: str) -> list[tuple[tuple[int, int], str]]:
    """``(span, term)`` of every day or span of days the text writes out."""
    out: list[tuple[tuple[int, int], str]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(a < span[1] and span[0] < b for (a, b), _t in out)

    def dateish(m: re.Match[str], month: str | None) -> bool:
        before = text[: m.start()]
        if _COUNTING.search(before):
            return False
        return not (month or "").lower().startswith("may") or bool(_MAY_DATE.search(before))

    def year(m: re.Match[str]) -> int | None:
        return int(m.group("y")) if m.group("y") else None

    for m in _DAY_RANGE_ISO.finditer(text):
        out.append((m.span(), f"{m.group('a')}..{m.group('b')}"))
    for m in _DAY_RANGE_DM.finditer(text):
        if free(m.span()) and dateish(m, m.group("m2")):
            d1, d2 = _day_num(m.group("d1")), _day_num(m.group("d2"))
            m1 = m.group("m1") or m.group("m2")
            term = _range_term(m1, d1, m.group("m2"), d2, m.group("y1"), m.group("y2"))
            out.append((m.span(), term))
    for m in _DAY_RANGE_MD.finditer(text):
        if free(m.span()) and dateish(m, m.group("m1")):
            d1, d2 = _day_num(m.group("d1")), _day_num(m.group("d2"))
            m2 = m.group("m2") or m.group("m1")
            term = _range_term(m.group("m1"), d1, m2, d2, m.group("y1"), m.group("y2"))
            out.append((m.span(), term))
    for m in _WEEK_OF.finditer(text):
        if not free(m.span()):
            continue
        if iso := m.group("iso"):
            out.append((m.span(), f"week:{iso}"))
            continue
        day = _md(m.group("m1") or m.group("m2"), _day_num(m.group("d1") or m.group("d2")))
        out.append((m.span(), f"week:{_dated(day, year(m))}"))
    for m in _HALF.finditer(text):
        if free(m.span()):
            lo, hi = (1, 15) if m.group("half").lower() in ("first", "1st") else (16, "end")
            month, y = m.group("m"), m.group("y")
            out.append((m.span(), _range_term(month, lo, month, hi, y, y)))
    for m in _FIRST_LAST.finditer(text):
        if not free(m.span()) or not dateish(m, m.group("m") if not m.group("y") else None):
            continue
        raw = (m.group("n") or "1").lower()
        n = _COUNT_WORDS.get(raw) or int(raw)
        if m.group("unit").lower().startswith("week"):
            n *= 7
        elif m.group("n") is None and m.group("unit").lower() == "days":
            continue  # "the first days of August" names no number: the qualifier asks
        month, y = m.group("m"), m.group("y")
        if m.group("which").lower() in ("first", "opening"):
            term = _range_term(month, 1, month, n, y, y)
        else:
            term = _range_term(month, f"end~{n - 1}", month, "end", y, y)
        out.append((m.span(), term))
    for m in _EDGE.finditer(text):
        if not free(m.span()) or not dateish(m, m.group("m") if not m.group("y") else None):
            continue
        yr: int | None
        if iso := m.group("iso"):
            y, mo = iso.split("-")
            md, yr = f"{mo}-01", int(y)
        else:
            md, yr = _md(m.group("m"), 1), year(m)
        if m.group("edge").lower() in ("start", "beginning"):
            out.append((m.span(), _dated(md, yr)))
        else:
            out.append((m.span(), "end:" + _dated(md[:2] + "-end", yr)))
    for m in _REL_EDGE.finditer(text):
        if not free(m.span()):
            continue
        if rel := m.group("rel"):
            rel = _REL_NORM.get(rel.lower(), rel.lower())
            edge = "start" if m.group("edge").lower() in ("start", "beginning") else "end"
            out.append((m.span(), f"{edge}:{rel} {m.group('grain').lower()}"))
            continue
        if _EACH_BEFORE.search(text[: m.start()]) or _names_a_period(text, m.span()):
            continue
        grain = (m.group("grain") or m.group("unit")).lower()
        if m.group("unit") or m.group("edge").lower() in ("end", "close"):
            # no period named: the last one that has ended (a week stays this one)
            out.append((m.span(), f"end:{'this' if grain == 'week' else 'last'} {grain}"))
        else:
            out.append((m.span(), f"start:this {grain}"))
    for m in _ONE_DAY.finditer(text):
        month = m.group("m1") or m.group("m2")
        if free(m.span()) and dateish(m, month):
            day = _md(month, _day_num(m.group("d1") or m.group("d2")))
            out.append((m.span(), _dated(day, year(m))))
    return out


# ── quarters and halves written in words ─────────────────────────────────────
# "the third quarter of 2026", "the 3rd quarter, 2026", "the second half of
# 2026", "the last quarter of 2026" (its fourth), "the first half of this year".
# Read as "q3"/"h2" beside the year, exactly as "Q3 2026" and "H2 2026" are;
# without a year ("the fourth quarter", "the first half of the year") the
# term is ``--q4``/``--h1`` and takes the most recent one starting on or
# before the lens clock (``window_ranges``). "last quarter" alone stays the
# relative period, and "the first half of the game" is no window.
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4}
_ORDINAL |= {"4th": 4, "last": 0, "final": 0}
_YEAR_PART = re.compile(
    rf"\b(?P<ord>{'|'.join(_ORDINAL)})\s+(?P<unit>quarter|half)"
    rf"(?:\s+(?:of|in)\s+(?:the\s+year\s+)?(?P<y>{_YR})|,?\s+(?P<y2>{_YR}))?(?![\w-])",
    re.IGNORECASE,
)
_REL_YEAR_AFTER = re.compile(
    r"\s+(?:of|in)\s+(?:the\s+)?(?:this|current|last|previous|prior|next)\s+year\b", re.I
)
_OF_THE_YEAR = re.compile(r"\s+of\s+the\s+year\b", re.I)
_OF_ANYTHING = re.compile(r"\s+of\b", re.I)


def _year_parts(text: str, taken: list[tuple[int, int]]) -> list[tuple[tuple[int, int], str]]:
    """``(span, term)`` of every quarter or half of a year written in words —
    a span ``taken`` already (a half of a month) is left alone. The span
    stops before a relative year ("of this year"), which reads as its own
    term and places the part in that year."""
    out: list[tuple[tuple[int, int], str]] = []
    for m in _YEAR_PART.finditer(text):
        if any(a < m.end() and m.start() < b for a, b in taken):
            continue
        n, unit = _ORDINAL[m.group("ord").lower()], m.group("unit").lower()
        year = m.group("y") or m.group("y2")
        after = text[m.end() :]
        relative = _REL_YEAR_AFTER.match(after) is not None
        if n == 0:
            if not (year or relative):
                continue  # "the last quarter" is the previous quarter
            n = 4 if unit == "quarter" else 2
        if unit == "half" and n > 2:
            continue
        part = f"{unit[0]}{n}"
        if year:
            out.append((m.span(), part))
            out.append(((m.end() - len(year), m.end()), year))
        elif relative:
            out.append((m.span(), part))
        elif of_year := _OF_THE_YEAR.match(after):
            out.append(((m.start(), m.end() + of_year.end()), f"--{part}"))
        elif not _OF_ANYTHING.match(after):
            out.append((m.span(), f"--{part}"))
    return out


def _phrases(text: str) -> list[tuple[tuple[int, int], str]]:
    """Every day phrase and every quarter or half written in words."""
    days = _day_phrases(text)
    return days + _year_parts(text, [span for span, _t in days])


def temporal_terms(text: str) -> frozenset[str]:
    """The normalized time-window terms a question states. Deliberately small:
    relative windows (this/last/next × grain, incl. 'last 30 days'), the *td
    shorthands, explicit quarters/months with years, month spans with a year
    ("January through August 2026" → one span term), days and spans of days
    written out ("on 28 August 2026" → 2026-08-28, "between 5 and 12 October"
    → --10-05..--10-12, resolved on the lens clock by ``window_ranges``), and
    bare years. A month name alone is NOT a window ('may' is a modal verb)."""
    out: set[str] = set()
    spanned: list[tuple[int, int]] = []
    for span, term in _phrases(text):
        out.add(term)
        spanned.append(span)
    for s in _SPAN.finditer(text):
        if any(a < s.end() and s.start() < b for a, b in spanned):
            continue
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
        elif hn := m.group("hn"):
            out.add(f"h{hn}")
            if m.group("hyear"):
                out.add(m.group("hyear"))
        elif month := m.group("month"):
            out.add(_month_name(month))
            out.add(m.group("myear"))
        elif iday := m.group("iday"):
            out.add(iday)
        elif iyear := m.group("iyear"):
            out.add(iyear)
            if iq := m.group("iq"):
                out.add(f"q{iq}")
            elif ih := m.group("ih"):
                out.add(f"h{ih}")
            else:
                out.add(_MONTH_BY_NUM[int(m.group("imonth"))])
        elif year := m.group("year"):
            out.add(year)
    return frozenset(out)


# A breakdown phrase: the question wants one figure PER calendar bucket.
# "by month", "per week", "each quarter", "end of every month", "at the end
# of each month", "per month-end", "a monthly trend", "MRR monthly for 2026".
# A bare "monthly" or "weekly" before a noun is not one: it names metrics
# ("monthly recurring revenue", "weekly active users") far more often than a
# series; the adverb counts only before a series noun, a period preposition,
# or the end of the question.
_BREAKDOWN = re.compile(
    r"\b(?:by|per|each|every|for\s+each|of\s+each|of\s+every)\s+"
    r"(?P<unit>week|month|quarter|year)s?\b"
    r"|\b(?P<adv>weekly|monthly|quarterly|yearly|annual)"
    r"(?:\s+(?:trend|breakdown|series|basis|progression|development|view)\b"
    r"|\s+(?:for|in|over|during|since|across|through|throughout|from|until|to)\b"
    r"|\s*[,.?!]|\s*$)",
    re.IGNORECASE,
)
_ADVERB_UNIT = {"weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year"}


def breakdown_unit(text: str) -> str | None:
    """The calendar unit a question asks a breakdown by (week, month, quarter,
    year), or None when it asks for one figure. Deterministic: the phrase
    alone, no reading of the rest of the sentence. A bare adverb before a
    noun is NOT read ("monthly recurring revenue" names a metric, not a
    series) — only "by/per <unit>" phrases and a trailing or punctuated
    adverb count."""
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
    spans = [span for span, _term in _phrases(text)]
    spans += [
        m.span()
        for m in _SPAN.finditer(text)
        if not any(a < m.end() and m.start() < b for a, b in spans)
    ]
    for m in _TEMPORAL.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return sorted(spans)


def beside_windows(text: str) -> str:
    """The text with every window phrase blanked to spaces (offsets kept):
    what the question says besides its period. "the first 2 days of
    October" is a window, so its 2 is never a top 2 and its "days" never a
    grouping by day."""
    for a, b in temporal_spans(text):
        text = text[:a] + " " * (b - a) + text[b:]
    return text


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
    days = [span for span, _term in _day_phrases(text)]
    out: list[str] = []
    for m in _MONTH_WORD.finditer(text):
        name = m.group("month").lower()
        if name == "may" or name in covered or name in out:
            continue
        if any(a <= m.start() < b for a, b in days):
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


_DAY_PART = r"(?:(?:19|20)\d{2}|-)-\d{2}-(?:\d{2}|end(?:~\d{1,2})?)"
_DAY_TERM = re.compile(rf"(?:week:|end:)?{_DAY_PART}(?:\.\.{_DAY_PART})?")


def _day_parts(part: str) -> tuple[int | None, int, str]:
    y, m, d = part.rsplit("-", 2)
    return (int(y) if y not in ("", "-") else None), int(m), d


def _day(y: int, m: int, d: str) -> date | None:
    if d.startswith("end"):
        # "end" is the month's last day; "end-2" two days before it
        if not 1 <= m <= 12:
            return None
        last = _month_end(y, m)
        back = int(d[4:] or 0) if d[3:4] in ("", "~") else -1
        if back < 0 or back >= last.day:
            return None
        return last - timedelta(days=back)
    try:
        return date(y, m, int(d))
    except ValueError:
        return None  # 2026-02-30 is not a day; unresolved, never a guess


def _day_span(term: str, today: date) -> tuple[date, date] | None:
    """The days a day term covers. A day or span written without a year is
    the most recent one starting on or before ``today``: "between 5 and 12
    October" asked on 22 October 2026 is 2026's, asked on 1 October it is
    2025's. "week:<day>" is the Monday-to-Sunday week holding that day;
    "end:<day>" is the last day of a month ("the end of August"), which a
    level metric reads as the month's last snapshot (``month_end_as_month``)."""
    week = term.startswith("week:")
    first, _sep, last = term.removeprefix("week:").removeprefix("end:").partition("..")
    y1, m1, d1 = _day_parts(first)
    y2, m2, d2 = _day_parts(last) if last else (y1, m1, d1)
    if y1 is None:
        this_year = _day(today.year, m1, d1)
        y1 = today.year if this_year is not None and this_year <= today else today.year - 1
        y2 = y1 + 1 if m2 < m1 else y1
    if y2 is None:
        return None
    start, end = _day(y1, m1, d1), _day(y2, m2, d2)
    if start is None or end is None or end < start:
        return None
    if week:
        start = start - timedelta(days=start.weekday())
        end = start + timedelta(days=6)
    return start, end


def month_end_as_month(terms: frozenset[str]) -> frozenset[str]:
    """``terms`` with each "end of <month>" read as the whole month: a level
    metric's figure at a month's end is the month's last snapshot, which is
    on its last day only when one is loaded there."""
    out: set[str] = set()
    for t in terms:
        if (edge := _EDGE_TERM.fullmatch(t)) and edge.group(1) == "end":
            out.add(t[4:])
        elif t.startswith("end:") and not edge:
            out.add(f"{t[4:].rsplit('-', 1)[0]}-01..{t[4:]}")
        else:
            out.add(t)
    return frozenset(out)


def _placed_month(m: re.Match[str], today: date) -> tuple[date, date]:
    """The month a phrase names, in its stated year or else the most recent
    one on or before ``today``'s month."""
    n = _MONTH_NUM[_month_name(m.group("m").rstrip("."))]
    y = int(m.group("y")) if m.group("y") else (today.year if n <= today.month else today.year - 1)
    return date(y, n, 1), _month_end(y, n)


def vague_days(text: str, today: date) -> list[tuple[str, date, date]]:
    """``(phrase, first, last)`` for each vague part of a month the text
    names — "early August" (1–10), "mid-August" (11–20), "late August" (21 to
    the end), "the middle of August" (11–20). These are never a window; the
    caller is asked, offered these days."""
    out: list[tuple[str, date, date]] = []
    for m in _VAGUE.finditer(text):
        if (m.group("m") or "").lower().startswith("may") and not m.group("y"):
            continue
        first, last = _placed_month(m, today)
        lo, hi = _VAGUE_DAYS[(m.group("v") or "mid").lower()]
        start = first.replace(day=int(lo))
        end = last if hi == "end" else first.replace(day=int(hi))
        out.append((m.group(0), start, end))
    return out


def unplaced_days(text: str, today: date) -> list[tuple[str, date, date]]:
    """``(phrase, month first, month last)`` for each day-level qualifier
    beside a month ("the first few days of August", "the week of August",
    "early August") that the window parser did not read as days. Served as
    the month, such a question gets a wider window than it asked; the caller
    of the window asks instead. "end of every month" names no month."""
    days = [span for span, _term in _day_phrases(text)]
    out: list[tuple[str, date, date]] = []
    for m in _QUALIFIER.finditer(text):
        if any(a <= m.start() < b or a < m.end() <= b for a, b in days):
            continue
        if (m.group("m") or "").lower().startswith("may") and not m.group("y"):
            continue
        first, last = _placed_month(m, today)
        out.append((m.group(0), first, last))
    return out


def is_day_window(terms: frozenset[str], *, relative: bool = True) -> bool:
    """Does a stated window name days — a date, a span of dates, a week of
    a date, and with ``relative`` today or yesterday — rather than whole
    months, quarters or years? Such a window served as its month is a
    different figure."""
    return any(
        _DAY_TERM.fullmatch(t)
        or _EDGE_TERM.fullmatch(t)
        or (relative and t in ("this day", "last day"))
        for t in terms
    )


_PART_TERM = re.compile(r"(?:--)?(?P<unit>[qh])(?P<n>[1-4])|(?:this|last|next) quarter")


def year_parts(terms: frozenset[str]) -> frozenset[int] | None:
    """The quarters of a year the stated quarters and halves cover ("h2" is
    {3, 4}; a relative quarter is one, numbered 0) — None when the terms
    name no quarter or half."""
    out: set[int] = set()
    for t in terms:
        if (m := _PART_TERM.fullmatch(t)) is None:
            continue
        if m.group("unit") is None:
            out.add(0)
        elif m.group("unit") == "q":
            out.add(int(m.group("n")))
        elif int(m.group("n")) <= 2:
            out.update({2 * int(m.group("n")) - 1, 2 * int(m.group("n"))})
    return frozenset(out) if out else None


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
    # a quarter or a half as its first and last month
    parts = [(3 * q - 2, 3 * q) for q in quarters]
    parts += sorted((6 * int(t[1]) - 5, 6 * int(t[1])) for t in terms if re.fullmatch(r"h[12]", t))
    years = sorted(int(t) for t in terms if re.fullmatch(r"(?:19|20)\d{2}", t))
    for t in sorted(t for t in terms if re.fullmatch(r"--(?:q[1-4]|h[12])", t)):
        # a quarter or half named without a year: the most recent one
        # starting on or before the lens clock
        k = int(t[3])
        m_first, m_last = (3 * k - 2, 3 * k) if t[2] == "q" else (6 * k - 5, 6 * k)
        y = today.year if date(today.year, m_first, 1) <= today else today.year - 1
        ranges.append((date(y, m_first, 1), _month_end(y, m_last)))
    for t in sorted(t for t in terms if _DAY_TERM.fullmatch(t)):
        if (days := _day_span(t, today)) is not None:
            ranges.append(days)
    for t in sorted(terms):
        if bound := _EDGE_TERM.fullmatch(t):
            p0, p1 = _calendar_period(today, bound.group(2), bound.group(3))
            ranges.append((p0, p0) if bound.group(1) == "start" else (p1, p1))
    for y in years:
        for m in months:
            ranges.append((date(y, m, 1), _month_end(y, m)))
        for m_from, m_to in spans:
            ranges.append((date(y, m_from, 1), _month_end(y, m_to)))
        for m_from, m_to in parts:
            ranges.append((date(y, m_from, 1), _month_end(y, m_to)))
        # A bare year stands alone only when no sub-year period was stated —
        # a span that did not resolve (backwards) still WAS stated.
        if not months and not parts and not span_terms:
            ranges.append((date(y, 1, 1), date(y, 12, 31)))
    for t in terms:
        rel_match = _REL_TERM.fullmatch(t)
        if rel_match is None:
            continue
        rel, n, grain = rel_match.group(1), rel_match.group(2), rel_match.group(3)
        if n is None and grain == "year" and parts:
            # "the third quarter of this year": the part, in that year
            y = _calendar_period(today, rel, grain)[0].year
            ranges += [(date(y, a, 1), _month_end(y, b)) for a, b in parts]
            continue
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
