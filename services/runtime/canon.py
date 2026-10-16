"""One reading of a result cell, shared by every rail that compares a stored
value with a live one: the eval gate, the serve-time ``restated`` check and
``--refresh-samples``. Two rails with two normalisations is how a timestamp's
text form ("2026-09-30T00:00:00" stored, "2026-09-30 00:00:00" read back)
became a restatement on one rail after the other had stopped seeing it."""

from __future__ import annotations

import datetime as _dt
import decimal
import re
from collections.abc import Mapping
from typing import Any

_NUMBER_TEXT = re.compile(r"^-?\d+(\.\d+)?$")


def canon_cell(v: Any) -> Any:
    """One cell in the shape the comparison reads: a number is a number
    whichever driver or store it came through (``Decimal`` and a stored
    "82.59" both read as 82.59), a date or timestamp is ISO text, with a
    midnight timestamp equal to its date ("2026-09-30 00:00:00",
    "2026-09-30T00:00:00" and "2026-09-30" are one day), and text compares
    without surrounding whitespace or case. A value a release before 0.11.1
    stored as text (``str(Decimal)``, ``str(datetime)``) therefore meets the
    live read as itself, never as a restatement."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, int | float):
        return v
    if isinstance(v, _dt.datetime):
        midnight = v.time() == _dt.time() and v.tzinfo is None
        return v.date().isoformat() if midnight else v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, str):
        text = v.strip()
        if _NUMBER_TEXT.match(text):
            return float(text) if "." in text else int(text)
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                parsed = _dt.datetime.fromisoformat(text)
            except ValueError:
                return text.casefold()
            return canon_cell(parsed)
        return text.casefold()
    return str(v)


def canon_rows(rows: list[list[Any]]) -> list[list[Any]]:
    return [[canon_cell(c) for c in r] for r in rows]


def canon_summary(summary: Mapping[str, Any] | None) -> Any:
    """A stored value summary (``{"value": x}`` or ``{"columns", "rows"}``)
    as a comparable structure: cells canonical, rows order-insensitive, key
    order and column case irrelevant. Two summaries that read the same data
    compare equal here whatever release wrote them."""
    if not summary:
        return None
    if "error" in summary:
        return ("error", str(summary["error"]))
    if set(summary) == {"value"}:
        return ("value", canon_cell(summary["value"]))
    columns = [str(c).lower() for c in summary.get("columns") or []]
    rows = canon_rows([list(r) for r in summary.get("rows") or []])
    return ("table", columns, sorted(rows, key=repr))
