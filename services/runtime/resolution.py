"""Build a ``Resolution`` for a served answer — by construction or by attribution.

Two doors, one vocabulary (services/contracts/resolution.py):

- ``from_intent``: the intent tier picked every name from the semantic model and
  the compiler resolved them, so metrics / definitions / dimensions / grain are
  ``declared`` by construction. Only filter VALUES need checking: a literal is
  declared when its column has a complete value dictionary that holds it.
- ``attribute``: the grounded tier (and an intent-tier escalation) wrote raw SQL,
  so the served SQL is read back with sqlglot. Everything that can be matched to
  a governed expression is ``declared``; every aggregate, GROUP BY member, literal
  or predicate that cannot is ``inferred``; SQL that does not parse is ``unknown``
  as a whole. Nothing is guessed: a sibling-metric tie no SELECT alias breaks is
  reported as the aggregate the SQL wrote, never as one of the siblings.

The matchers are the verification / attribution ones, reused rather than forked:
one reading of "is this expression applied in that SQL" across the badge, the
basis line and the ledger.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from services.contracts.query_intent import QueryIntent
from services.contracts.resolution import MEASURE_KINDS, Resolution, Slot, derive_tag
from services.contracts.semantic_model import Entity, SemanticModel
from services.runtime import shape_guard, verification
from services.runtime.compiler import CompileError, metric_sql

Domains = dict[str, list[str]]

_TIME_TYPES = {"date", "timestamp"}


# ── shared model lookups ─────────────────────────────────────────────────────


def _time_fields(model: SemanticModel) -> set[str]:
    """Every field name the model treats as a time axis (lowercased)."""
    out: set[str] = set()
    for e in model.entities:
        if e.default_time_field:
            out.add(e.default_time_field.lower())
        out.update(m.agg_time_field.lower() for m in e.metrics if m.agg_time_field)
        out.update(f.name.lower() for f in e.fields if f.type in _TIME_TYPES)
    return out


def _dimension_exprs(model: SemanticModel) -> dict[str, str]:
    """authored dimension expression (normalised) -> dimension name."""
    return {
        verification._norm(d.expr): d.name
        for e in model.entities
        for d in e.dimensions
        if d.expr and d.expr.strip()
    }


def _entity_present(entity: Entity, sql: str) -> bool:
    """Does the SQL name this entity's table (or the entity itself)? A table the
    SQL never reads cannot have contributed a column — the cross-entity guard."""
    table = entity.source.table.rsplit(".", 1)[-1]
    return verification._touches(table, sql) or verification._touches(entity.name, sql)


def _literal_declared(field: str, literals: list[str], domains: Domains | None) -> bool:
    domain = (domains or {}).get(field.lower()) or []
    return bool(domain) and all(lit in domain for lit in literals)


def _bare(col: exp.Column) -> str:
    return col.name.lower()


# ── by construction ──────────────────────────────────────────────────────────


def from_intent(
    intent: QueryIntent,
    model: SemanticModel,
    domains: Domains | None,
    supplied: list[str] | None = None,
) -> Resolution:
    """The compiler resolved these names or raised — they are the provenance.
    ``supplied`` names the filter columns whose value the caller bound."""
    slots: list[Slot] = []
    slots += [Slot(kind="metric", name=m, source="declared") for m in intent.metrics]
    slots += [Slot(kind="definition", name=d, source="declared") for d in intent.definitions]
    slots += [Slot(kind="dimension", name=d, source="declared") for d in intent.dimensions]
    slots += [Slot(kind="dimension", name=f, source="declared") for f in intent.fields]
    supplied_cols = {s.lower() for s in (supplied or [])}
    if intent.grain:
        entity = next((e for e in model.entities if e.name == intent.entity), None)
        field = entity.default_time_field if entity else None
        for e in model.entities:
            for m in e.metrics:
                if m.name in intent.metrics and m.agg_time_field:
                    field = m.agg_time_field
        slots.append(
            Slot(kind="grain", name=field or "time", source="declared", value=intent.grain)
        )
    time_fields = _time_fields(model)
    bool_fields = {f.name.lower() for e in model.entities for f in e.fields if f.type == "boolean"}
    governed = _governed_predicates(model, set(intent.metrics), model.dialect)
    for f in intent.filters:
        field = f.field.rsplit(".", 1)[-1]
        values = f.value if isinstance(f.value, list) else [f.value]
        text = f"{f.field} {f.op} {f.value!r}"
        canon = _canon_text(f"{field} {f.op} {_sql_literal(f.value)}", model.dialect)
        if canon and any(canon in g for g in governed):
            # The filter restates a governed predicate (the entity's population
            # filter, a definition): it IS that meaning, not an invented filter.
            continue
        if field.lower() in time_fields:
            slots.append(Slot(kind="window", name=field, source="inferred", value=text))
            continue
        strings = [v for v in values if isinstance(v, str)]
        declared = (
            f.op in ("=", "in")
            and len(strings) == len(values)
            and _literal_declared(field, strings, domains)
        ) or (
            # A boolean field's domain is closed by its type: true/false is a
            # declared value, never a guess.
            field.lower() in bool_fields and all(isinstance(v, bool) for v in values)
        )
        source: str = "declared" if declared else "inferred"
        if field.lower() in supplied_cols:
            source = "supplied"
        slots.append(Slot(kind="filter", name=field, source=source, value=text))  # type: ignore[arg-type]
    return Resolution(method="construction", slots=slots, tag=derive_tag(slots))


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "(" + ", ".join(_sql_literal(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


# ── by attribution ───────────────────────────────────────────────────────────


def _unknown() -> Resolution:
    return Resolution(
        method="attributed",
        slots=[Slot(kind="metric", name="", source="unknown")],
        tag="unknown",
    )


def _governed_metrics(model: SemanticModel) -> list[tuple[str, str]]:
    """``(metric name, compiled expression)`` in both the bare and filter-inlined
    forms (pipeline._governed_expressions' metric half; the served SQL may embed either)."""
    pairs: list[tuple[str, str]] = []
    for entity in model.entities:
        for metric in entity.metrics:
            for inline in (False, True):
                try:
                    expr = metric_sql(metric, entity, inline_filters=inline)
                except CompileError:
                    break
                pairs.append((metric.name, expr))
                if "COUNT(1)" in expr:
                    # A plain count is stored as COUNT(1) (the spelling that survives
                    # the filter path); generated SQL writes COUNT(*). Same metric.
                    pairs.append((metric.name, expr.replace("COUNT(1)", "COUNT(*)")))
    return pairs


def _aliased(term: str, sql: str) -> bool:
    return re.search(rf'\bas\s+"?{re.escape(term)}"?\b', sql, re.I) is not None


def _matched_metrics(model: SemanticModel, sql: str, sql_norm: str) -> tuple[list[Slot], list[str]]:
    """Declared metric slots + the matched expressions (to subtract from the
    aggregates the outer SELECT still carries). Sibling metrics that compile to
    the SAME expression tie; a SELECT alias breaks the tie, otherwise the tie is
    reported as the expression itself, inferred — never a guessed sibling."""
    from services.runtime.pipeline import _tables_are_present

    by_expr: dict[str, list[str]] = {}
    for term, expr in _governed_metrics(model):
        if verification._expr_in_sql(expr, sql, sql_norm) and _tables_are_present(expr, sql):
            names = by_expr.setdefault(expr, [])
            if term not in names:
                names.append(term)
    slots: list[Slot] = []
    matched_exprs: list[str] = []
    seen: set[str] = set()
    for expr, terms in by_expr.items():
        matched_exprs.append(expr)
        if len(terms) > 1:
            terms = [t for t in terms if _aliased(t, sql)] or []
            if not terms:
                slots.append(Slot(kind="metric", name=expr, source="inferred"))
                continue
        for t in terms:
            if t not in seen:
                seen.add(t)
                slots.append(Slot(kind="metric", name=t, source="declared"))
    return slots, matched_exprs


def _matched_definitions(model: SemanticModel, sql: str, sql_norm: str) -> list[Slot]:
    from services.runtime.pipeline import _tables_are_present

    return [
        Slot(kind="definition", name=d.term, source="declared", value=d.sql_expr)
        for d in model.definitions
        if d.sql_expr
        and d.sql_expr.strip()
        and verification._expr_in_sql(d.sql_expr, sql, sql_norm)
        and _tables_are_present(d.sql_expr, sql)
    ]


def _canon(node: exp.Expr) -> str:
    node = node.copy()
    for col in node.find_all(exp.Column):
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    for ident in node.find_all(exp.Identifier):
        ident.set("quoted", False)
    return verification._norm(node.sql())


def _canon_text(fragment: str, dialect: str) -> str | None:
    try:
        node = sqlglot.parse_one(fragment, read=dialect)
    except Exception:  # noqa: BLE001 — an unparseable fragment governs nothing here
        return None
    return _canon(node) if node is not None else None


def _governed_predicates(
    model: SemanticModel, matched_metrics: set[str], dialect: str
) -> list[str]:
    """Canonical text of every predicate that is PART of a governed meaning: a
    definition's sql_expr, a matched metric's filters, an entity's population
    filter. A WHERE clause inside one of these is the definition, not a filter
    the generator invented."""
    frags: list[str] = [d.sql_expr for d in model.definitions if d.sql_expr]
    for e in model.entities:
        if e.population_filter:
            frags.append(e.population_filter)
        for m in e.metrics:
            if m.name in matched_metrics:
                frags.extend(m.filters)
    return [c for f in frags if (c := _canon_text(f, dialect))]


def _projection_nodes(select: exp.Select) -> list[exp.Expr]:
    return [p.this if isinstance(p, exp.Alias) else p for p in select.expressions]


def _group_nodes(select: exp.Select) -> list[exp.Expr]:
    group = select.args.get("group")
    if group is None:
        return []
    projections = _projection_nodes(select)
    out: list[exp.Expr] = []
    for g in group.expressions:
        if isinstance(g, exp.Literal) and g.is_int:
            idx = int(g.this) - 1
            if 0 <= idx < len(projections):
                out.append(projections[idx])
            continue
        if isinstance(g, exp.Column) and not g.table:
            # GROUP BY <alias> — resolve to the aliased projection when there is one.
            alias = next(
                (
                    p.this
                    for p in select.expressions
                    if isinstance(p, exp.Alias) and p.alias == g.name
                ),
                None,
            )
            out.append(alias if alias is not None else g)
            continue
        out.append(g)
    return out


def _time_field_slot(node: exp.Expr, model: SemanticModel, sql: str) -> Slot:
    col = node.find(exp.Column)
    unit = node.args.get("unit")
    grain = (unit.name if unit is not None else "").lower() or None
    name = _bare(col) if col is not None else node.sql()
    declared = (
        col is not None
        and name in _time_fields(model)
        and _column_on_present_entity(name, model, sql)
    )
    return Slot(
        kind="grain",
        name=name,
        source="declared" if declared else "inferred",
        value=grain,
    )


def _column_on_present_entity(name: str, model: SemanticModel, sql: str) -> bool:
    for e in model.entities:
        members = {d.name.lower() for d in e.dimensions} | {f.name.lower() for f in e.fields}
        if name in members and _entity_present(e, sql):
            return True
    return False


def _dimension_slots(select: exp.Select, model: SemanticModel, sql: str) -> list[Slot]:
    slots: list[Slot] = []
    dim_exprs = _dimension_exprs(model)
    for node in _group_nodes(select):
        if isinstance(node, exp.DateTrunc | exp.TimestampTrunc):
            slots.append(_time_field_slot(node, model, sql))
            continue
        if isinstance(node, exp.Column):
            name = _bare(node)
            declared = _column_on_present_entity(name, model, sql)
            slots.append(
                Slot(
                    kind="dimension",
                    name=name if declared else node.sql(),
                    source="declared" if declared else "inferred",
                )
            )
            continue
        authored = dim_exprs.get(_canon(node))
        if authored:
            slots.append(Slot(kind="dimension", name=authored, source="declared"))
        else:
            slots.append(Slot(kind="dimension", name=node.sql(), source="inferred"))
    return slots


def _inferred_aggregates(select: exp.Select, matched_exprs: list[str]) -> list[Slot]:
    """Aggregates in the outer SELECT no governed expression accounts for."""
    slots: list[Slot] = []
    for node in _projection_nodes(select):
        aggs = list(node.find_all(exp.AggFunc))
        if not aggs:
            continue
        text = node.sql()
        norm = verification._norm(text)
        if any(verification._expr_in_sql(expr, text, norm) for expr in matched_exprs):
            continue
        for agg in aggs:
            slots.append(Slot(kind="metric", name=agg.sql(), source="inferred"))
    return slots


def _split_and(cond: exp.Expr) -> list[exp.Expr]:
    if isinstance(cond, exp.And):
        return _split_and(cond.left) + _split_and(cond.right)
    if isinstance(cond, exp.Paren):
        return _split_and(cond.this)
    return [cond]


def _string_literals(pred: exp.Expr) -> tuple[exp.Column, list[str]] | None:
    """``(column, literals)`` for an ``=`` / ``IN`` over string literals, else None."""
    if isinstance(pred, exp.EQ):
        column, literal = pred.this, pred.expression
        if isinstance(literal, exp.Column) and isinstance(column, exp.Literal):
            column, literal = literal, column
        if (
            isinstance(column, exp.Column)
            and isinstance(literal, exp.Literal)
            and literal.is_string
        ):
            return column, [str(literal.this)]
    if isinstance(pred, exp.In):
        column = pred.this
        literals = [
            str(e.this) for e in pred.expressions if isinstance(e, exp.Literal) and e.is_string
        ]
        if isinstance(column, exp.Column) and literals and len(literals) == len(pred.expressions):
            return column, literals
    return None


def _filter_slots(
    stmt: exp.Expr,
    model: SemanticModel,
    domains: Domains | None,
    governed: list[str],
) -> list[Slot]:
    slots: list[Slot] = []
    time_fields = _time_fields(model)
    bool_fields = {f.name.lower() for e in model.entities for f in e.fields if f.type == "boolean"}
    seen: set[str] = set()
    for where in stmt.find_all(exp.Where):
        for pred in _split_and(where.this):
            canon = _canon(pred)
            if canon in seen:
                continue
            seen.add(canon)
            if any(canon in g for g in governed):
                continue  # this predicate IS a definition / metric filter / population bound
            text = pred.sql()
            col = pred.find(exp.Column)
            name = _bare(col) if col is not None else text
            if col is not None and name in time_fields:
                slots.append(Slot(kind="window", name=name, source="inferred", value=text))
                continue
            if (
                isinstance(pred, exp.EQ)
                and isinstance(pred.this, exp.Column)
                and isinstance(pred.expression, exp.Boolean)
                and _bare(pred.this) in bool_fields
            ):
                slots.append(
                    Slot(kind="filter", name=_bare(pred.this), source="declared", value=text)
                )
                continue
            strings = _string_literals(pred)
            if strings is not None:
                column, literals = strings
                declared = _literal_declared(_bare(column), literals, domains)
                slots.append(
                    Slot(
                        kind="filter",
                        name=_bare(column),
                        source="declared" if declared else "inferred",
                        value=text,
                    )
                )
                continue
            slots.append(Slot(kind="filter", name=name, source="inferred", value=text))
    return slots


def attribute(
    sql: str, model: SemanticModel, domains: Domains | None = None, dialect: str | None = None
) -> Resolution:
    """Read the served SQL back into slots. Unparseable SQL is ``unknown`` — one
    slot, tag unknown — and never a guess."""
    if not sql or not sql.strip():
        return _unknown()
    read = dialect or model.dialect
    try:
        stmt = sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001 — the ledger reports unknown, it does not raise
        return _unknown()
    if stmt is None:
        return _unknown()
    outer = stmt.this if isinstance(stmt, exp.SetOperation) else stmt
    if not isinstance(outer, exp.Select):
        return _unknown()

    sql_norm = verification._norm(sql)
    # A declared ratio written inline (SUM(...) * 1.0 / COUNT(*)) IS that ratio:
    # it consumes its operands, so they are read back as the ratio and the rest
    # of the SQL is matched without them — never as numerator and denominator
    # standing in for it.
    ratio_slots: list[Slot] = []
    metric_sql_text, metric_outer = sql, outer
    present = [e for e in model.entities if _entity_present(e, sql)]
    work = stmt.copy()
    composed = (
        shape_guard.composed_ratios(work, present, read) if isinstance(work, exp.Expression) else []
    )
    if composed:
        for name, div in composed:
            if name not in {s.name for s in ratio_slots}:
                ratio_slots.append(Slot(kind="metric", name=name, source="declared"))
            div.replace(exp.Null())
        metric_sql_text = work.sql(dialect=read)
        rewritten = work.this if isinstance(work, exp.SetOperation) else work
        if isinstance(rewritten, exp.Select):
            metric_outer = rewritten
    metric_slots, matched_exprs = _matched_metrics(
        model, metric_sql_text, verification._norm(metric_sql_text)
    )
    metric_slots = [
        *ratio_slots,
        *(s for s in metric_slots if s.name not in {r.name for r in ratio_slots}),
    ]
    definition_slots = _matched_definitions(model, sql, sql_norm)
    slots: list[Slot] = [
        *metric_slots,
        *_inferred_aggregates(metric_outer, matched_exprs),
        *definition_slots,
    ]
    slots += _dimension_slots(outer, model, sql)
    # A grain that lives in a projection but not in GROUP BY (a scalar
    # DATE_TRUNC) is still the axis the answer resolved to.
    if not any(s.kind == "grain" for s in slots):
        for node in _projection_nodes(outer):
            trunc = node.find(exp.DateTrunc, exp.TimestampTrunc)
            if trunc is not None:
                slots.append(_time_field_slot(trunc, model, sql))
                break
    matched_metric_names = {s.name for s in metric_slots if s.source == "declared"}
    governed = _governed_predicates(model, matched_metric_names, read)
    slots += _filter_slots(stmt, model, domains, governed)
    return Resolution(method="attributed", slots=slots, tag=derive_tag(slots))


# ── certified overlay ────────────────────────────────────────────────────────


def certified_overlay(res: Resolution) -> Resolution:
    """A human approved the whole query: every slot is certified, whatever
    attribution found. Attribution still ran so the slots have NAMES — the eval
    lane compares those — but the source is the approval's."""
    return Resolution(
        method=res.method,
        slots=[
            s.model_copy(update={"source": "certified"}) for s in res.slots if s.source != "unknown"
        ],
        tag="certified",
        # Certified SQL is typed by approval; the decisions that matched it
        # (the paraphrase gate) still ride the ledger.
        decisions=list(res.decisions),
        typed=True,
    )


# ── the eval lane ────────────────────────────────────────────────────────────

GOVERNED = {"declared", "certified"}


def _governed_names(res: Resolution, kinds: set[str]) -> set[tuple[str, str]]:
    return {(s.kind, s.name) for s in res.slots if s.kind in kinds and s.source in GOVERNED}


def grade(expected: Resolution, got: Resolution | None) -> tuple[str, str]:
    """``(passed | failed | skipped, evidence)`` — did generation resolve to the
    same GOVERNED names the oracle SQL resolves to?

    Only declared/certified names compare: an inferred slot's name is SQL text,
    and grading SQL text against SQL text is the one thing the harness must
    never do (rows are the truth of correctness; this lane grades meaning).
    Measures, dimensions and grain compare; literal filters and windows do
    not — same question, same window, but "'2026-01-01'" vs "DATE '2026-01-01'"
    is formatting, not meaning. An oracle that names nothing governed grades
    nothing: ``skipped`` with the reason, never a clean pass (rule 4).
    """
    if expected.tag == "unknown":
        return "skipped", "oracle SQL could not be attributed"
    if got is None or got.tag == "unknown":
        return "skipped", "generated SQL could not be attributed"
    kinds = {"metric", "definition", "dimension", "grain"}
    want, have = _governed_names(expected, kinds), _governed_names(got, kinds)
    if not want:
        return "skipped", "oracle SQL names no declared metric, definition or dimension"
    # Subset, not equality: a human-written oracle often sums a raw column
    # where generation used the declared metric. Generation resolving to MORE
    # governed names than the oracle is not a miss — the miss is a governed
    # name the oracle carries and generation did not.
    if want <= have:
        want_grain = {
            s.value or "" for s in expected.slots if s.kind == "grain" and s.source in GOVERNED
        }
        have_grain = {
            s.value or "" for s in got.slots if s.kind == "grain" and s.source in GOVERNED
        }
        if want_grain != have_grain:
            return "failed", f"grain: expected {sorted(want_grain)}, got {sorted(have_grain)}"
        raw = [s.name for s in expected.slots if s.kind in MEASURE_KINDS and s.source == "inferred"]
        if raw and not any(
            s.kind in MEASURE_KINDS and s.source in GOVERNED for s in expected.slots
        ):
            # Said, not hidden: the CERTIFIED SQL computes its figure over raw
            # columns; generation resolved it to a declared metric. An authoring
            # note for the certifier, never a strike against generation.
            return "passed", f"certified SQL computes {', '.join(raw)} raw; generation used " + (
                ", ".join(sorted(n for k, n in have - want if k in MEASURE_KINDS))
                or "a declared name"
            )
        return "passed", ""

    def _show(names: set[tuple[str, str]], res: Resolution) -> str:
        governed = ", ".join(sorted(n for _k, n in names)) or "nothing declared"
        invented = [s.name for s in res.slots if s.kind in kinds and s.source == "inferred"]
        return governed + (f" (inferred: {', '.join(invented)})" if invented else "")

    return "failed", f"expected {_show(want, expected)}; got {_show(have, got)}"
