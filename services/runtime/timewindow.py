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

_REL_NORM = {"current": "this", "previous": "last", "prior": "last", "past": "last"}
_MTD = {"ytd": "this year", "qtd": "this quarter", "mtd": "this month"}
_TEMPORAL = re.compile(
    r"\b(?:"
    r"(?P<rel>this|current|last|previous|prior|next|past|trailing)\s+"
    r"(?:(?P<n>\d+)\s+)?(?P<grain>day|week|month|quarter|year)s?"
    r"|(?P<word>today|yesterday|ytd|qtd|mtd)"
    r"|q(?P<qn>[1-4])(?:\s*(?:of\s*)?(?P<qyear>(?:19|20)\d{2}))?"
    r"|(?P<month>january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+(?P<myear>(?:19|20)\d{2})"
    r"|(?P<year>(?:19|20)\d{2})"
    r")\b",
    re.IGNORECASE,
)


def temporal_terms(text: str) -> frozenset[str]:
    """The normalized time-window terms a question states. Deliberately small:
    relative windows (this/last/next × grain, incl. 'last 30 days'), the *td
    shorthands, explicit quarters/months with years, and bare years. A month
    name alone is NOT a window ('may' is a modal verb)."""
    out: set[str] = set()
    for m in _TEMPORAL.finditer(text):
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
            out.add(month.lower())
            out.add(m.group("myear"))
        elif year := m.group("year"):
            out.add(year)
    return frozenset(out)


def temporal_mismatch(asked: str, approved: str) -> bool:
    """True only when both questions state a window and the windows differ."""
    a, b = temporal_terms(asked), temporal_terms(approved)
    return bool(a) and bool(b) and a != b


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
