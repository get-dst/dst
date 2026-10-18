"""time_guard — deterministic grain check for time-bucketed grouping.

An entity's ``default_time_field`` is its canonical event-time column; when it
is TIMESTAMP-typed, GROUPing BY the RAW column is a per-event grain wearing a
per-period costume. "sessions per month in 2025" then serves ~5000
one-row groups, and the model agrees the SQL was wrong when asked.
Same disease as the filter bug: the prompt's bucketing line is
advisory. This module puts the check in code so the pipeline can repair (name
the DATE_TRUNC fix) or reject before a five-thousand-row "monthly" answer is
served.

A DATE-typed field raw-grouped is legal daily grain — until the question
asks for a coarser unit ("monthly", "by month", "one row per month"), or the
same GROUP BY also carries a bucketed form of the column: ``GROUP BY
DATE_TRUNC('month', day), day`` is a no-op bucket, and a "monthly" answer then
serves every day. Both are deterministic reads (``timewindow.breakdown_unit``
and the GROUP BY shape), so a DATE column is a target under either, and only
then. ``grain_applied`` is the result-side half: the served rows must hold
one time value per asked bucket.

Bias, by filter_guard's explicit contract: UNDER-detection is acceptable,
FALSE POSITIVES ARE NOT. Hence: TIMESTAMP-typed default_time_fields always,
DATE-typed ones only under an asked unit or a bucket beside them, only the OUTERMOST query's
GROUP BY (an inner raw grouping can be deliberate pre-aggregation), only bare
column references (any wrapping function or cast counts as bucketed), strict
entity attribution (unqualified columns only in single-table scope), and a
group that also carries a primary-key column of the entity is row grain on
purpose (dedupe) — never flagged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime

import sqlglot
from sqlglot import exp

# Table-name matching reuses the certified-bindings idiom, same as filter_guard.
from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import Entity, SemanticModel
from services.contracts.verification import VerificationCheck
from services.runtime.filter_guard import _alias_map
from services.runtime.timewindow import breakdown_unit

# Bucketing periods recognisable in a question, finest first — "daily sessions
# this year" asks for days, not years. Default when none is named: month.
_PERIODS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bhour(?:s|ly)?\b"), "hour"),
    (re.compile(r"\bda(?:y|ys|ily)\b"), "day"),
    (re.compile(r"\bweek(?:s|ly)?\b"), "week"),
    (re.compile(r"\bmonth(?:s|ly)?\b"), "month"),
    (re.compile(r"\bquarter(?:s|ly)?\b"), "quarter"),
    (re.compile(r"\b(?:year(?:s|ly)?|annual(?:ly)?)\b"), "year"),
]


def asked_period(question: str) -> str:
    """The DATE_TRUNC period the question asks for ('month' when it names none)."""
    q = question.lower()
    for pattern, period in _PERIODS:
        if pattern.search(q):
            return period
    return "month"


def _resolved(node: exp.Expression, projections: list[exp.Expression]) -> exp.Expression:
    """What a GROUP BY expression actually groups by: ordinals resolve to the
    projection at that position, bare names to the projection they alias,
    aliases and parens unwrapped."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_int:
        idx = int(node.name) - 1
        if 0 <= idx < len(projections):
            node = projections[idx]
    if isinstance(node, exp.Column) and not node.table:
        for p in projections:
            if isinstance(p, exp.Alias) and p.alias and p.alias.lower() == node.name.lower():
                node = p
                break
    while isinstance(node, exp.Paren | exp.Alias):
        node = node.this
    return node


def _attributed(
    col: exp.Column, entity: Entity, alias_map: dict[str, set[str]], sole_source: bool
) -> bool:
    """Does *col* provably belong to *entity*? Strict, the false-positive-proof
    direction: every resolution candidate must match (an ambiguous alias never
    attributes), unqualified columns only in single-table scope."""
    if col.db or col.catalog:
        refs = {".".join(p.lower() for p in (col.catalog, col.db, col.table) if p)}
    elif col.table:
        refs = alias_map.get(col.table.lower(), {col.table.lower()})
    else:
        return sole_source
    ent = entity.name.lower()
    return all(r == ent or _ref_matches(r, entity.source.table) for r in refs)


def raw_timestamp_groupings(
    sql: str, model: SemanticModel, question: str | None = None
) -> list[tuple[str, str]]:
    """``[(entity_name, column_name), ...]`` for each default_time_field the
    outermost query provably GROUPs BY raw where raw is the wrong grain: a
    TIMESTAMP column always; a DATE column when *question* asks a unit coarser
    than a day, or when the same GROUP BY also buckets that column (the raw
    column beside its DATE_TRUNC makes the bucket a no-op). Empty when the
    grain contract holds (or nothing is checkable)."""
    unit = breakdown_unit(question) if question else None
    coarse_ask = unit is not None and asked_period(question or "") not in ("hour", "day")
    targets: list[tuple[Entity, str, bool]] = []
    for entity in model.entities:
        dtf = entity.default_time_field
        if not dtf:
            continue
        declared = next((f for f in entity.fields if f.name == dtf), None)
        if declared is not None and declared.type in ("timestamp", "date"):
            targets.append((entity, dtf, declared.type == "date"))
    if not targets:
        return []
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's rejection, not ours
        return []
    if not isinstance(stmt, exp.Select):
        return []  # set operations and friends — under-detect, never guess
    group = stmt.args.get("group")
    if group is None:
        return []
    alias_map = _alias_map(stmt)
    all_refs = set().union(*alias_map.values()) if alias_map else set()
    projections = list(stmt.expressions)
    resolved = [_resolved(g, projections) for g in group.expressions]

    out: list[tuple[str, str]] = []
    for entity, dtf, is_date in targets:
        sole = bool(all_refs) and all(_ref_matches(r, entity.source.table) for r in all_refs)
        pk = {p.lower() for p in entity.primary_key}
        if any(
            isinstance(n, exp.Column)
            and n.name.lower() in pk
            and _attributed(n, entity, alias_map, sole)
            for n in resolved
        ):
            continue  # grouping at row grain on purpose (dedupe / pre-aggregation)
        bucketed_too = any(
            not isinstance(n, exp.Column)
            and any(
                c.name.lower() == dtf.lower() and _attributed(c, entity, alias_map, sole)
                for c in n.find_all(exp.Column)
            )
            for n in resolved
        )
        if is_date and not (coarse_ask or bucketed_too):
            continue  # a DATE column raw-grouped is legal daily grain
        for node in resolved:
            if (
                isinstance(node, exp.Column)
                and node.name.lower() == dtf.lower()
                and _attributed(node, entity, alias_map, sole)
                and (entity.name, dtf) not in out
            ):
                out.append((entity.name, dtf))
    return out


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")


def _as_date(value: object) -> date | None:
    """A date, datetime or ISO-formatted string as a date; None for anything else."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        m = _ISO_DATE.match(value.strip())
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    return None


def _bucket(d: date, unit: str) -> tuple[int, ...]:
    if unit == "week":
        return d.isocalendar()[:2]
    if unit == "month":
        return (d.year, d.month)
    if unit == "quarter":
        return (d.year, (d.month - 1) // 3)
    return (d.year,)


def grain_applied(
    question: str, columns: Sequence[str], rows: Sequence[Sequence[object]], certification: str
) -> VerificationCheck:
    """Do the served rows hold one time value per asked bucket? A question
    asking a breakdown unit ("by month") answered with a time column that
    holds several values inside one bucket (the 1st, the 2nd, ... of a
    month) was served at a finer grain than asked, whatever the SQL says —
    the compiled ``GROUP BY DATE_TRUNC('MONTH', day), day`` passed every
    SQL-side check and served 273 daily rows under `verified`.

    Reads the result only, from the question's deterministic phrase: skips
    when no unit is asked, no column holds dates, or the SQL is certified (an
    approval binds the reading, as with level_grain). Fails only when some
    time column holds more than twice as many distinct values as buckets — a
    month-end label per bucket, or one jittered snapshot date per group,
    stays below that line (under-detect, never a false positive)."""
    unit = breakdown_unit(question)
    if unit is None:
        return VerificationCheck(
            name="grain_applied", status="skip", reason="the question asks no breakdown unit"
        )
    if certification == "certified":
        return VerificationCheck(
            name="grain_applied", status="skip", reason="certified SQL: approval binds the grain"
        )
    checked = 0
    for i, name in enumerate(columns):
        values = [row[i] for row in rows if i < len(row) and row[i] is not None]
        if not values:
            continue
        dates = [_as_date(v) for v in values]
        if any(d is None for d in dates):
            continue
        checked += 1
        distinct = {d for d in dates if d is not None}
        buckets = {_bucket(d, unit) for d in distinct}
        if len(distinct) > 2 * len(buckets):
            return VerificationCheck(
                name="grain_applied",
                status="fail",
                reason=(
                    f"the question asks one row per {unit} but column '{name}' holds "
                    f"{len(distinct)} distinct dates across {len(buckets)} {unit}(s) — the "
                    f"result is at a finer grain than asked (bucket the time column with "
                    f"DATE_TRUNC('{unit}', ...) and group by nothing finer)"
                ),
            )
    if not checked:
        return VerificationCheck(
            name="grain_applied", status="skip", reason="no time column in the result"
        )
    return VerificationCheck(name="grain_applied", status="pass")
