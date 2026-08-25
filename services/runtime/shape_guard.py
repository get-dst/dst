"""shape_guard — deterministic refusal of composed ungoverned metric shapes.

Name-based exclusion (pipeline.deterministic_exclusion) refuses a question that
ASKS for a dropped metric by name, but the composed-shape path slips through:
a lens whose selection carried only ``session_count`` served a self-composed
conversion rate — COUNT GROUPed BY the converted flag, division in prose,
``confidence: unverified``, three times over. Refusal beats a low-confidence
answer, so this module
recognizes, via sqlglot canonical forms (filter_guard's own machinery — one
canonicalization, one twin doctrine), when SQL composes the SHAPE of a metric
the shared layer defines but this lens's selection dropped
(SemanticModel.excluded_metric_shapes), so the pipeline can refuse with the
path instead of serving unverified. The ``serve_ungoverned_shapes`` lens knob
skips BOTH checks — by-name and composed-shape — letting the question run and
serve at ``confidence: unverified``; both entry points also share ONE refusal
text (``refusal_reason`` — the by-name path passes ``named=True``), so the
doctrine cannot fork.

A dropped SIMPLE metric is composed when its canonical aggregation appears
with its governed filters satisfied at that node (bare, when it has none). A
dropped RATIO metric is composed when a denominator-shaped aggregation appears
alongside its numerator — as a second aggregation node, or in grouped form:
the denominator's aggregation GROUPed BY the numerator's filter column(s),
which hands the model both operands as rows to divide in prose.

Bias, inherited from filter_guard and sharpened by its twin lesson: a shape a
SELECTED metric legitimately owns is NEVER flagged. An aggregation matching a
selected metric (filters satisfied — vacuously for an unfiltered one, which
also legitimizes its filtered narrowings: slicing a selected metric by a
selected field is ordinary query-writing) is that metric, not a
reconstruction; a ratio both of whose components are selected is two selected
metrics side by side. Matching an excluded metric demands TIGHT evidence
(node-local conditions: the aggregation's own guard, FILTER clause, and its
own SELECT's WHERE), while clearing via a selected twin stays generous
(conditions anywhere in the statement). Known unchecked forms: dropped
derived metrics, ratio components that are themselves ratios, COUNT(*)
stand-ins, and filters satisfied only in a feeding subquery — under-detection
is acceptable; refusing a legitimately selected shape is not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import sqlglot
from sqlglot import exp

from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import Entity, Metric, SemanticModel
from services.runtime.compiler import metric_sql
from services.runtime.filter_guard import (
    _CONDITION_HOMES,
    _AggKey,
    _alias_map,
    _canon,
    _conjuncts,
    _unwrap_guard,
)


@dataclass
class _Identity:
    """A simple metric's canonical identity: aggregation key + filter conjuncts.

    ``checkable`` is False when any filter fragment failed to parse or attribute
    — such a metric can still CLEAR a node (generous, filter_guard's rule) but
    never claims one (tight)."""

    key: _AggKey
    conjs: list[str]
    cols: set[str]  # canonical columns the filters reference (the grouped-form flag)
    checkable: bool


@dataclass
class _Hit:
    """One aggregation node matching some identity's key, with the two
    satisfaction sets the doctrine needs — tight (claiming an excluded shape)
    and generous (clearing via a selected twin) — plus its own SELECT's
    canonical GROUP BY columns."""

    node: exp.Expression
    local: set[str]
    cleared: set[str]
    grouped: set[str]


def _identity(metric: Metric, entity: Entity, dialect: str) -> _Identity | None:
    """Canonical identity for a simple metric, or None when the aggregation
    itself can't be canonicalized (skip: under-detect, never guess)."""
    if metric.type != "simple":
        return None
    try:
        agg = sqlglot.parse_one(metric_sql(metric, entity, inline_filters=False), read=dialect)
    except Exception:  # noqa: BLE001 — a malformed metric is validate's problem
        return None
    inner = agg.this
    distinct = isinstance(inner, exp.Distinct)
    if distinct:
        if len(inner.expressions) != 1:
            return None
        inner = inner.expressions[0]
    canon_inner = _canon(inner, entity, {}, True, strict=True)
    if canon_inner is None:
        return None
    conjs: list[str] = []
    cols: set[str] = set()
    checkable = True
    for frag in metric.filters:
        try:
            parsed = sqlglot.parse_one(frag, read=dialect)
        except Exception:  # noqa: BLE001 — unparseable fragment: uncheckable
            checkable = False
            continue
        if not isinstance(parsed, exp.Expression):
            checkable = False
            continue
        canon = [
            _canon(c, entity, {}, True, strict=True, condition=True) for c in _conjuncts(parsed)
        ]
        col_canon = [_canon(c, entity, {}, True, strict=True) for c in parsed.find_all(exp.Column)]
        if any(c is None for c in canon) or any(c is None for c in col_canon):
            checkable = False
            continue
        conjs.extend(c for c in canon if c is not None)
        cols.update(c for c in col_canon if c is not None)
    agg_type = cast(type[exp.Expression], type(agg))
    return _Identity((agg_type, distinct, canon_inner), conjs, cols, checkable)


def _grouped_cols(
    select: exp.Select | None,
    entity: Entity,
    alias_map: dict[str, set[str]],
    sole: bool,
) -> set[str]:
    """Canonical entity-attributed columns a SELECT groups by — ordinals resolved
    against its projections, aliases unwrapped. Unattributable expressions are
    simply not credited (crediting is what creates a refusal here)."""
    group = select.args.get("group") if select is not None else None
    if select is None or group is None:
        return set()
    out: set[str] = set()
    for g in group.expressions:
        if isinstance(g, exp.Literal) and not g.is_string:  # GROUP BY 1
            try:
                g = select.expressions[int(g.name) - 1]
            except (ValueError, IndexError):
                continue
        if isinstance(g, exp.Alias):
            g = g.this
        c = _canon(g, entity, alias_map, sole, strict=True)
        if c is not None:
            out.add(c)
    return out


def _entity_composed(
    stmt: exp.Expression,
    entity: Entity,
    dropped: list[Metric],
    alias_map: dict[str, set[str]],
    all_refs: set[str],
    dialect: str,
) -> tuple[list[str], list[str]]:
    """(composed ratio names, composed simple names) for one entity's dropped
    shapes against one parsed statement."""
    sole = bool(all_refs) and all(_ref_matches(r, entity.source.table) for r in all_refs)

    def lenient(conds: list[exp.Expression]) -> set[str]:
        canon = (_canon(c, entity, alias_map, sole, strict=False, condition=True) for c in conds)
        return {s for s in canon if s is not None}

    # Conditions that CLEAR a node via a selected twin — anywhere in the
    # statement, filter_guard's generous rule (the cost of over-crediting is
    # a missed refusal, never a blocked selected shape).
    global_conds: set[str] = set()
    for home in _CONDITION_HOMES:
        for clause in stmt.find_all(home):
            global_conds |= lenient(_conjuncts(clause.this))
    for join in stmt.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            global_conds |= lenient(_conjuncts(on))

    by_name = {m.name: m for m in dropped} | {m.name: m for m in entity.metrics}
    selected_names = {m.name for m in entity.metrics}
    idents = {
        name: ident
        for name, m in by_name.items()
        if (ident := _identity(m, entity, dialect)) is not None
    }

    # One node scan per distinct aggregation key any identity needs.
    hits: dict[_AggKey, list[_Hit]] = {}
    for key in {i.key for i in idents.values()}:
        agg_type, distinct, canon_inner = key
        for node in stmt.find_all(agg_type):
            arg = node.this
            if distinct != isinstance(arg, exp.Distinct):
                continue
            if isinstance(arg, exp.Distinct):
                if len(arg.expressions) != 1:
                    continue
                arg = arg.expressions[0]
            inner, guard_conds = _unwrap_guard(arg)
            if isinstance(node.parent, exp.Filter):  # AGG(x) FILTER (WHERE ...)
                fw = node.parent.expression
                if isinstance(fw, exp.Where):
                    guard_conds = [*guard_conds, *_conjuncts(fw.this)]
            cand = _canon(inner, entity, alias_map, sole, strict=True)
            if cand is None or cand != canon_inner:
                continue  # not (provably) this shape — under-detect, never guess
            select = node.find_ancestor(exp.Select)
            where = select.args.get("where") if select is not None else None
            own_where = _conjuncts(where.this) if isinstance(where, exp.Where) else []
            hits.setdefault(key, []).append(
                _Hit(
                    node,
                    local=lenient([*guard_conds, *own_where]),
                    cleared=global_conds | lenient(guard_conds),
                    grouped=_grouped_cols(select, entity, alias_map, sole),
                )
            )

    def claims(ident: _Identity, hit: _Hit) -> bool:
        return ident.checkable and all(c in hit.local for c in ident.conjs)

    def selected_clears(hit: _Hit, key: _AggKey) -> bool:
        return any(
            i.key == key and all(c in hit.cleared for c in i.conjs)
            for name in selected_names
            if (i := idents.get(name)) is not None
        )

    # Ratios first: the composed ratio is the user-facing ask (the caller names
    # conversion_rate, not its ingredients).
    ratios: list[str] = []
    simples: list[str] = []
    for metric in dropped:
        if metric.type != "ratio" or not (metric.numerator and metric.denominator):
            continue
        if metric.numerator == metric.denominator:
            continue
        if metric.numerator in selected_names and metric.denominator in selected_names:
            continue  # two selected metrics side by side — legitimate
        num = idents.get(metric.numerator)
        den = idents.get(metric.denominator)
        if num is None or den is None or not (num.checkable and den.checkable):
            continue
        # A node claiming the numerator's narrower conditions IS the numerator
        # — it never doubles as the denominator.
        den_hits = [
            h
            for h in hits.get(den.key, [])
            if claims(den, h) and not (num.conjs and claims(num, h))
        ]
        if not den_hits:
            continue
        pair = any(h is not d for h in hits.get(num.key, []) if claims(num, h) for d in den_hits)
        grouped = bool(num.cols) and any(num.cols <= d.grouped for d in den_hits)
        if pair or grouped:
            ratios.append(metric.name)

    for metric in dropped:
        if metric.type != "simple":
            continue  # dropped derived metrics: documented under-detection
        ident = idents.get(metric.name)
        if ident is None or metric.name in selected_names:
            continue
        for hit in hits.get(ident.key, []):
            if claims(ident, hit) and not selected_clears(hit, ident.key):
                simples.append(metric.name)
                break
    return ratios, simples


def composed_excluded_shapes(sql: str, model: SemanticModel) -> list[str]:
    """Names of dropped shared-layer metrics whose SHAPE *sql* composes —
    ratios before simples, each once, empty when the selection boundary holds.

    A name here becomes a refusal, so every heuristic leans the same way as
    filter_guard's: strict attribution, tight evidence to claim an excluded
    shape, generous clearing by any selected twin."""
    if not model.excluded_metric_shapes:
        return []
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's rejection, not ours
        return []
    if not isinstance(stmt, exp.Expression):
        return []
    alias_map = _alias_map(stmt)
    all_refs = set().union(*alias_map.values()) if alias_map else set()

    ratios: list[str] = []
    simples: list[str] = []
    for entity in model.entities:
        dropped = model.excluded_metric_shapes.get(entity.name) or []
        if not dropped:
            continue
        r, s = _entity_composed(stmt, entity, dropped, alias_map, all_refs, model.dialect)
        ratios.extend(r)
        simples.extend(s)
    return list(dict.fromkeys([*ratios, *simples]))


def refusal_reason(
    names: list[str], carriers: Callable[[str], list[str]] | None, *, named: bool = False
) -> str:
    """The decline-with-path refusal — ONE text for BOTH entry points of the
    selection boundary (the doctrine must not fork): name each
    metric, the file-first fix, and — when EXACTLY ONE other lens in the org
    carries the metric — that lens by name. ``named=True`` is the pre-generation
    entry (pipeline.deterministic_exclusion: the question ASKS for the metric by
    name); the default lead-in is the composed-shape path. ``carriers`` is
    consulted lazily (refusals only) and a broken lookup never breaks the
    refusal."""
    parts: list[str] = []
    for name in names:
        how = (
            f"the question asks for '{name}' by name"
            if named
            else f"the answer would compose '{name}' from raw columns"
        )
        part = (
            f"{how}, but this lens's selection does not carry it — add '{name}' "
            "to this lens's selection in lens.yaml or certify this answer"
        )
        others: list[str] = []
        if carriers is not None:
            try:
                others = list(carriers(name))
            except Exception:  # noqa: BLE001 — a hint must never break a refusal
                others = []
        if len(others) == 1:
            part += f" (lens '{others[0]}' carries '{name}')"
        parts.append(part)
    return "ungoverned metric shape: " + "; ".join(parts)
