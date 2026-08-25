"""time_guard — deterministic grain check for time-bucketed grouping.

An entity's ``default_time_field`` is its canonical event-time column; when it
is TIMESTAMP-typed, GROUPing BY the RAW column is a per-event grain wearing a
per-period costume. "sessions per month in 2025" then serves ~5000
one-row groups, and the model agrees the SQL was wrong when asked.
Same disease as the filter bug: the prompt's bucketing line is
advisory. This module puts the check in code so the pipeline can repair (name
the DATE_TRUNC fix) or reject before a five-thousand-row "monthly" answer is
served.

Bias, by filter_guard's explicit contract: UNDER-detection is acceptable,
FALSE POSITIVES ARE NOT. Hence: only TIMESTAMP-typed default_time_fields (a
DATE column raw-grouped is legal daily grain), only the OUTERMOST query's
GROUP BY (an inner raw grouping can be deliberate pre-aggregation), only bare
column references (any wrapping function or cast counts as bucketed), strict
entity attribution (unqualified columns only in single-table scope), and a
group that also carries a primary-key column of the entity is row grain on
purpose (dedupe) — never flagged.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# Table-name matching reuses the certified-bindings idiom, same as filter_guard.
from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import Entity, SemanticModel
from services.runtime.filter_guard import _alias_map

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


def raw_timestamp_groupings(sql: str, model: SemanticModel) -> list[tuple[str, str]]:
    """``[(entity_name, column_name), ...]`` for each TIMESTAMP-typed
    default_time_field the outermost query provably GROUPs BY raw — empty when
    the grain contract holds (or nothing is checkable)."""
    targets: list[tuple[Entity, str]] = []
    for entity in model.entities:
        dtf = entity.default_time_field
        if not dtf:
            continue
        declared = next((f for f in entity.fields if f.name == dtf), None)
        if declared is not None and declared.type == "timestamp":
            targets.append((entity, dtf))
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
    for entity, dtf in targets:
        sole = bool(all_refs) and all(_ref_matches(r, entity.source.table) for r in all_refs)
        pk = {p.lower() for p in entity.primary_key}
        if any(
            isinstance(n, exp.Column)
            and n.name.lower() in pk
            and _attributed(n, entity, alias_map, sole)
            for n in resolved
        ):
            continue  # grouping at row grain on purpose (dedupe / pre-aggregation)
        for node in resolved:
            if (
                isinstance(node, exp.Column)
                and node.name.lower() == dtf.lower()
                and _attributed(node, entity, alias_map, sole)
                and (entity.name, dtf) not in out
            ):
                out.append((entity.name, dtf))
    return out
