"""Deterministic metric-layer compiler: QueryIntent + SemanticModel -> dialect SQL.

Every metric/dimension/filter name is resolved against the model; an unknown name raises
CompileError (the caller falls back to raw-SQL generation). Aggregations, GROUP BY, and
dialect syntax are produced by code, not the LLM — so they can't be hallucinated.

Scope: metrics come from ONE entity; dimensions, filters and ordering may live on any
entity reachable from it over the model's declared ``joins`` (multi-hop, shortest path).
Each table is aliased AS its entity name so the model's ``entity.column`` expressions
resolve.

Fan-out is decided HERE rather than checked afterwards. Joining the many side of a
one-to-many multiplies the base rows and silently inflates every additive aggregate —
deposits by region over a per-customer SCD reports a total several times the true one.
That defect is not detectable from the finished SQL, because whether the duplication is
a bug depends on the question's intended grain: the same SQL shape is correct at one
grain and wildly wrong at another. At compile time the grain IS
known — the intent names it — so a hop that would fan the base out is refused instead,
and the pipeline falls back to the raw-SQL path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.semantic_model import Entity, Join, Metric, SemanticModel
from services.runtime.identifiers import is_reserved, quote_ident, quote_table

_AGG = {"sum": "SUM", "count": "COUNT", "avg": "AVG", "min": "MIN", "max": "MAX"}
_COMPARISON = {"=", "!=", ">", "<", ">=", "<="}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class CompileError(Exception):
    """The intent could not be compiled against the model (unknown name, ambiguous, …)."""


def _resolve_entity(model: SemanticModel, name: str | None, metrics: list[str]) -> Entity:
    if name:
        for e in model.entities:
            if e.name == name:
                return e
        raise CompileError(f"unknown entity '{name}'")
    if len(model.entities) == 1:
        return model.entities[0]
    if metrics:
        owning = [e for e in model.entities if {m.name for m in e.metrics} >= set(metrics)]
        if len(owning) == 1:
            return owning[0]
        # No entity carries all of them. Saying "ambiguous entity" here blames the
        # wrong thing — the caller usually misspelled a metric, and the useful
        # sentence names it. (Reached the moment the metrics door made these
        # messages caller-facing.)
        known = {m.name for e in model.entities for m in e.metrics}
        unknown = [m for m in metrics if m not in known]
        if unknown:
            raise CompileError(
                f"unknown metric {', '.join(repr(m) for m in unknown)} — this lens carries: "
                + (", ".join(sorted(known)) or "no metrics")
            )
        if not owning:
            raise CompileError(
                f"no single entity carries all of {', '.join(sorted(metrics))} — metrics "
                "from different entities cannot be combined in one intent"
            )
    with_metrics = [e for e in model.entities if e.metrics]
    if len(with_metrics) == 1:
        return with_metrics[0]
    raise CompileError("ambiguous entity — specify one")


def metric_sql(metric: Metric, entity: Entity, *, inline_filters: bool = True) -> str:
    """The full SQL aggregate expression for a metric, expanded against its entity.

    Simple: ``AGG(expr)``, filters inlined as CASE WHEN when ``inline_filters``
    (used when selected metrics carry DIFFERENT filters, where a shared WHERE
    would intersect them — completed AND returned = zero rows, silently).
    Ratio/derived expand each referenced sibling metric recursively; references
    ALWAYS inline their filters (a shared WHERE would poison the other operand),
    and a ratio wraps as CAST(num AS DOUBLE) / NULLIF(denom, 0). Unknown
    references and reference cycles raise CompileError.
    """
    return _expand(metric, {m.name: m for m in entity.metrics}, [], (), inline_filters)


def _ref(name: str, by_name: dict[str, Metric], owner: str) -> Metric:
    ref = by_name.get(name)
    if ref is None:
        raise CompileError(f"metric '{owner}' references unknown sibling metric '{name}'")
    return ref


def _expand(
    metric: Metric,
    by_name: dict[str, Metric],
    inherited: list[str],
    seen: tuple[str, ...],
    inline_filters: bool,
) -> str:
    if metric.name in seen:
        chain = " -> ".join((*seen, metric.name))
        raise CompileError(f"metric '{metric.name}' is defined in terms of itself ({chain})")
    seen = (*seen, metric.name)
    if metric.type == "simple":
        assert metric.agg is not None and metric.expr is not None  # pydantic enforces
        expr = metric.expr
        conds = [*inherited, *(metric.filters if inline_filters else [])]
        if conds:
            expr = f"CASE WHEN {' AND '.join(conds)} THEN {expr} END"
        if metric.agg == "count_distinct":
            return f"COUNT(DISTINCT {expr})"
        return f"{_AGG[metric.agg]}({expr})"
    filters = [*inherited, *metric.filters]
    if metric.type == "ratio":
        assert metric.numerator is not None and metric.denominator is not None
        num = _expand(_ref(metric.numerator, by_name, metric.name), by_name, filters, seen, True)
        den = _expand(_ref(metric.denominator, by_name, metric.name), by_name, filters, seen, True)
        return f"CAST({num} AS DOUBLE) / NULLIF({den}, 0)"
    assert metric.expr is not None  # derived

    def _sub(m: re.Match[str]) -> str:
        ref = _ref(m.group(1), by_name, metric.name)
        return "(" + _expand(ref, by_name, filters, seen, True) + ")"

    return _PLACEHOLDER.sub(_sub, metric.expr)


def foreign_entity_refs(
    fragments: Sequence[str | None], *, owner: str, entities: set[str], dialect: str
) -> list[str]:
    """`entity.column` references in *fragments* owned by an entity other than *owner*.

    Only qualifiers that NAME ANOTHER ENTITY count. An unknown qualifier is left
    alone on purpose: `payload.value` is struct access in BigQuery, not a broken
    reference, and a lint that cannot tell them apart would reject valid models.

    The ONE seam for "which other entities does this expression reach into":
    ``compile_intent`` uses it to pull those entities into the FROM clause, and
    ``validate`` uses it to reject the ones no declared join can reach. Two
    readings of the same question is how a metric was authorable, validated and
    still compiled to SQL naming an untabled alias.
    """
    found: list[str] = []
    for frag in fragments:
        if not frag:
            continue
        try:
            tree = sqlglot.parse_one(f"SELECT {frag}", read=dialect)
        except Exception:  # noqa: BLE001 — the parse gates elsewhere own bad syntax
            continue
        for col in tree.find_all(exp.Column):
            qualifier = col.table
            if qualifier and qualifier != owner and qualifier in entities:
                ref = f"{qualifier}.{col.name}"
                if ref not in found:
                    found.append(ref)
    return found


def metric_entity_refs(
    metric: Metric, entity: Entity, model: SemanticModel
) -> dict[str, list[str]]:
    """``{other entity: the entity.column refs of it}`` *metric*'s own SQL reaches into.

    Read off the EXPANDED aggregate (``metric_sql`` with filters inlined), so a
    ratio inherits whatever its operands reference — ``average_purchase_price``
    is dollars_spent/quantity_bought, and only the expansion says the first of
    those touches ``bitcoin_prices``. The raw ``filters`` ride along too: a
    derived metric with no ``{sibling}`` placeholder has nowhere to push them
    down into, so they never reach the expansion. An unexpandable metric (bad
    sibling ref, cycle) still yields its filters — ``metric_ref_invalid`` is
    that error's own gate, and this one must not go quiet waiting for it.
    """
    try:
        expanded: str | None = metric_sql(metric, entity, inline_filters=True)
    except CompileError:
        expanded = None
    names = {e.name for e in model.entities}
    out: dict[str, list[str]] = {}
    for ref in foreign_entity_refs(
        [expanded, *metric.filters], owner=entity.name, entities=names, dialect=model.dialect
    ):
        out.setdefault(ref.partition(".")[0], []).append(ref)
    return out


def _qualify(entity: Entity, name: str, dialect: str) -> str:
    if "." in name:
        return name  # an authored expression, already qualified by its author
    return f"{quote_ident(entity.name, dialect)}.{quote_ident(name, dialect)}"


def _member_expr(entity: Entity, name: str, dialect: str) -> str | None:
    for d in entity.dimensions:
        if d.name == name:
            return d.expr or _qualify(entity, d.name, dialect)
    for f in entity.fields:
        if f.name == name:
            return _qualify(entity, f.name, dialect)
    return None


def _resolve_dim(entity: Entity, name: str, dialect: str) -> str:
    expr = _member_expr(entity, name, dialect)
    if expr is None:
        raise CompileError(f"unknown dimension/field '{name}'")
    return expr


def _resolve_member(
    model: SemanticModel, base: Entity, name: str, dialect: str
) -> tuple[Entity, str]:
    """Resolve a dimension/field name to its owning entity and SQL expression.

    Accepts ``entity.member`` to disambiguate. Bare names prefer the base entity —
    a fact table's own column is what a question means by it — and a bare name
    carried by two OTHER entities is an error rather than a coin flip.
    """
    owner, _, member = name.rpartition(".")
    if owner:
        ent = next((e for e in model.entities if e.name == owner), None)
        if ent is None:
            raise CompileError(f"unknown entity '{owner}' in '{name}'")
        expr = _member_expr(ent, member, dialect)
        if expr is None:
            raise CompileError(f"unknown dimension/field '{member}' on entity '{owner}'")
        return ent, expr

    own = _member_expr(base, name, dialect)
    if own is not None:
        return base, own
    matches = [
        (e, expr)
        for e in model.entities
        if e.name != base.name and (expr := _member_expr(e, name, dialect)) is not None
    ]
    if not matches:
        raise CompileError(f"unknown dimension/field '{name}'")
    if len(matches) > 1:
        owners = ", ".join(f"{e.name}.{name}" for e, _ in matches)
        raise CompileError(f"ambiguous dimension/field '{name}' — qualify it: {owners}")
    return matches[0]


def _fans_out(join: Join, frm: str) -> bool:
    """Does adding the other side of *join* to a row set containing *frm* duplicate it?

    ``Join(left=L, right=R, relationship=rel)`` states the cardinality L->R. Travelling
    L->R duplicates L when one L maps to many R (``one_to_many``); travelling R->L
    duplicates R when one R maps to many L, which is the same fact written the other way
    round (``many_to_one``). An undeclared relationship is treated as unsafe: guessing is
    how a metric layer reports seven times the truth.
    """
    if join.relationship == "one_to_one":
        return False
    if join.relationship is None:
        return True
    return join.relationship == ("one_to_many" if join.left == frm else "many_to_one")


def _join_path(model: SemanticModel, base: str, target: str) -> list[tuple[Join, str]]:
    """Shortest declared path base -> target as [(join, entity_being_joined_from), ...]."""
    if base == target:
        return []
    adjacency: dict[str, list[tuple[str, Join]]] = {}
    for j in model.joins:
        adjacency.setdefault(j.left, []).append((j.right, j))
        adjacency.setdefault(j.right, []).append((j.left, j))
    queue: list[tuple[str, list[tuple[Join, str]]]] = [(base, [])]
    seen = {base}
    while queue:
        node, path = queue.pop(0)
        for nxt, join in adjacency.get(node, []):
            if nxt in seen:
                continue
            step = [*path, (join, node)]
            if nxt == target:
                return step
            seen.add(nxt)
            queue.append((nxt, step))
    raise CompileError(
        f"no declared join path from '{base}' to '{target}' — add a joins: entry, or the "
        "question spans entities this lens does not connect"
    )


def _lit(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _time_bucket(
    entity: Entity, metrics: list[Metric], grain: str, dialect: str
) -> tuple[str, str]:
    """``(alias, sql)`` for the entity's time field truncated to *grain*.

    The field is the metric's ``agg_time_field`` when it overrides, else the entity's
    ``default_time_field``. The expression is built as a sqlglot node rather than a
    DATE_TRUNC string because the dialects genuinely differ — BigQuery takes the unit
    second, and MySQL has no DATE_TRUNC at all (sqlglot emits the STR_TO_DATE form).

    This is the whole of what time_guard exists to catch: grouping an over-time
    question on a raw TIMESTAMP is per-event grain wearing a per-period costume, and
    on the compiled path it now cannot happen.
    """
    # Compare EFFECTIVE time fields, not just the overrides: a metric without an
    # override still has one (the entity default), so `revenue` (order_date) beside
    # `shipped` (shipped_at) is a real disagreement. Reading only the overrides made
    # that pair silently bucket revenue by the shipping date.
    effective = {m.agg_time_field or entity.default_time_field for m in metrics}
    if len(effective) > 1:
        named = ", ".join(sorted(f or "(none)" for f in effective))
        raise CompileError(
            f"metrics disagree about which time field to bucket ({named}) — ask for them separately"
        )
    field = next(iter(effective), None) if effective else entity.default_time_field
    if not field:
        raise CompileError(
            f"entity '{entity.name}' declares no default_time_field, so there is nothing "
            f"to bucket by {grain} — add `default_time_field:` to the entity, or group by "
            "a date dimension explicitly"
        )
    if not any(f.name == field for f in entity.fields):
        raise CompileError(
            f"entity '{entity.name}' names '{field}' as its time field, but has no such "
            "field — fix default_time_field/agg_time_field"
        )
    # Quoting is per-identifier (the entity may need it while the field does not),
    # so build the column from two identifiers rather than one `quoted=` flag.
    column = exp.column(
        exp.to_identifier(field, quoted=is_reserved(field, dialect)),
        table=exp.to_identifier(entity.name, quoted=is_reserved(entity.name, dialect)),
    )
    node = exp.DateTrunc(this=column, unit=exp.Literal.string(grain))  # type: ignore[no-untyped-call]
    return f"{field}_{grain}", node.sql(dialect=dialect)


def unsafe_join_reason(model: SemanticModel, base: str, target: str) -> str | None:
    """Why *base* cannot soundly reach *target* over declared joins — None when it can.

    The ONE reading of that question. ``_join_plan`` refuses on it at compile time
    (falling back to raw SQL); ``validate`` asks the same function at AUTHORING
    time, so a metric reaching across a join can never be accepted by one and
    rejected by the other. Two refusals: no declared path, and a hop that fans the
    base out (an undeclared ``relationship:`` counts as fanning — guessing is how a
    metric layer reports seven times the truth).
    """
    try:
        path = _join_path(model, base, target)
    except CompileError as exc:
        return str(exc)
    for join, src in path:
        if _fans_out(join, src):
            other = join.right if join.left == src else join.left
            return (
                f"joining '{other}' to '{src}' duplicates every '{src}' row "
                f"(declared {join.relationship or 'no'} relationship), which would "
                f"inflate the metrics — aggregate '{other}' upstream, or declare the "
                "relationship if it does not actually fan out"
            )
    return None


def _join_plan(
    model: SemanticModel, base: Entity, needed: Sequence[Entity]
) -> list[tuple[Join, str]]:
    """The joins to emit, in an order where every ON clause references a joined entity.

    Raises CompileError when a hop would fan the base out — see the module docstring:
    at this point the intent still says what one output row means, so refusing is a
    decision we can make correctly, and the raw-SQL path takes over.
    """
    plan: list[tuple[Join, str]] = []
    emitted: set[int] = set()
    for target in needed:
        if target.name == base.name:
            continue
        if (reason := unsafe_join_reason(model, base.name, target.name)) is not None:
            raise CompileError(reason)
        for join, src in _join_path(model, base.name, target.name):
            if id(join) not in emitted:
                emitted.add(id(join))
                plan.append((join, src))
    return plan


def _definition_sql(model: SemanticModel, term: str) -> str:
    """The governed predicate a named definition contributes, or a CompileError.

    A definition with no ``sql_expr`` is glossary only — it explains a term, it does
    not enforce one — so naming it as a filter is a mistake the caller should hear,
    not a silent no-op. Unknown term and empty predicate both fall the whole compile
    back to raw SQL, where the definition body still governs."""
    for d in model.definitions:
        if d.term == term:
            if not (d.sql_expr or "").strip():
                raise CompileError(
                    f"definition '{term}' has no sql_expr to apply as a filter — it is "
                    "glossary only"
                )
            assert d.sql_expr is not None  # guarded above
            return d.sql_expr
    raise CompileError(f"unknown definition '{term}'")


def _predicate(col: str, f: IntentFilter, dialect: str) -> str:
    if f.op == "in":
        vals = f.value if isinstance(f.value, list) else [f.value]
        return f"{col} IN ({', '.join(_lit(v) for v in vals)})"
    if f.op == "like":
        return f"{col} LIKE {_lit(f.value)}"
    if f.op in _COMPARISON:
        return f"{col} {f.op} {_lit(f.value)}"
    raise CompileError(f"unsupported operator '{f.op}'")


def compile_intent(intent: QueryIntent, model: SemanticModel) -> str:
    # Before resolving anything: an empty intent has no entity to be ambiguous
    # about, and "ambiguous entity" is a confusing way to say "you asked for nothing".
    if not intent.metrics and not intent.dimensions:
        raise CompileError("intent selects neither a metric nor a dimension")

    entity = _resolve_entity(model, intent.entity, intent.metrics)
    metrics_by = {m.name: m for m in entity.metrics}

    for m in intent.metrics:
        if m not in metrics_by:
            raise CompileError(f"unknown metric '{m}'")

    # Metric filters are part of the metric's MEANING (avg won ACV only
    # over won deals). One shared filter set → a plain WHERE (index-friendly).
    # DIFFERENT filter sets → each metric filters inside its own aggregation
    # (CASE form); a shared WHERE would intersect them into zero rows, silently.
    # Any ratio/derived metric forces the CASE form: its expansion self-contains
    # every filter, and a shared WHERE would leak into its operands.
    filter_sets = {tuple(metrics_by[m].filters) for m in intent.metrics}
    compound = any(metrics_by[m].type != "simple" for m in intent.metrics)
    mixed = len(filter_sets) > 1 or compound
    shared_metric_filters = list(next(iter(filter_sets))) if intent.metrics and not mixed else []

    # Every identifier this function emits is quoted when a bare emission would be
    # misread (cold-start: an entity named `order` — one of the commonest table
    # names there is — compiled to `FROM orders AS order` and every answer through
    # it died on a warehouse parser error). Authored expressions are left alone:
    # they are the author's SQL, quoted or not as they wrote them.
    dialect = model.dialect
    # Everything the intent names, resolved to (owning entity, SQL expression). Doing
    # this before emitting anything means an unknown or ambiguous name fails the whole
    # compile — the caller falls back — rather than half-building a query.
    bucket: tuple[str, str] | None = (
        _time_bucket(entity, [metrics_by[m] for m in intent.metrics], intent.grain, dialect)
        if intent.grain
        else None
    )
    dim_exprs: list[tuple[Entity, str]] = [
        _resolve_member(model, entity, d, dialect) for d in intent.dimensions
    ]
    filter_exprs: list[tuple[Entity, str]] = [
        _resolve_member(model, entity, f.field, dialect) for f in intent.filters
    ]
    # A named definition contributes its authored sql_expr VERBATIM — the compiler
    # owns the predicate, the model only names which one, so an edited definition
    # changes the served answer and no phrasing can escape it. Resolved before any
    # SQL is emitted, like every other name: an unknown definition fails the whole
    # compile and the pipeline falls back to raw SQL.
    definition_exprs: list[str] = [_definition_sql(model, term) for term in intent.definitions]
    # An ORDER BY on a selected alias needs nothing resolved; anything else is a real
    # member reference and can pull in its own entity, exactly like a filter.
    selected_aliases = set(metrics_by) | set(intent.dimensions) | ({bucket[0]} if bucket else set())
    order_exprs: list[tuple[Entity, str] | None] = [
        None if o.field in selected_aliases else _resolve_member(model, entity, o.field, dialect)
        for o in intent.order_by
    ]

    select: list[str] = []
    group: list[str] = []
    if bucket:
        # The time bucket leads the SELECT: a period column is what a trend is read
        # along, and putting it first is what every BI consumer expects.
        select.append(f"{bucket[1]} AS {quote_ident(bucket[0], dialect)}")
        group.append(bucket[1])
    for d, (_owner, expr) in zip(intent.dimensions, dim_exprs, strict=True):
        select.append(f"{expr} AS {quote_ident(d, dialect)}")
        group.append(expr)
    for m in intent.metrics:
        select.append(
            f"{metric_sql(metrics_by[m], entity, inline_filters=mixed)} "
            f"AS {quote_ident(m, dialect)}"
        )

    sql = (
        f"SELECT {', '.join(select)} FROM {quote_table(entity.source.table, dialect)} "
        f"AS {quote_ident(entity.name, dialect)}"
    )
    # A metric's OWN expression may reach into a joined entity
    # (e.g. dollars_spent = quantity * prices.price across a declared
    # many_to_one). Those entities belong in the FROM clause exactly
    # like a dimension's — reading only dimensions/filters/ORDER BY emitted the
    # reference with no table to bind it to. _join_plan still refuses the hop
    # when it would fan the base out, so nothing unsound gets joined here.
    by_name = {e.name: e for e in model.entities}
    metric_entities = [
        by_name[name]
        for m in intent.metrics
        for name in metric_entity_refs(metrics_by[m], entity, model)
    ]
    # A definition predicate may reach into a joined entity (`active customer =
    # regions.tier = 'gold'`); pull those in exactly like a metric's own refs, so
    # the WHERE never names an untabled alias. A hop that would fan the base out is
    # still refused by _join_plan below → CompileError → raw-SQL fallback.
    definition_refs = foreign_entity_refs(
        definition_exprs, owner=entity.name, entities=set(by_name), dialect=dialect
    )
    definition_entities = [by_name[ref.partition(".")[0]] for ref in definition_refs]
    needed = (
        [o for o, _ in (*dim_exprs, *filter_exprs, *[x for x in order_exprs if x])]
        + metric_entities
        + definition_entities
    )
    joined_entities = [entity]
    for join, src in _join_plan(model, entity, needed):
        other = join.right if join.left == src else join.left
        target = next(e for e in model.entities if e.name == other)
        joined_entities.append(target)
        sql += (
            f" {join.type.upper()} JOIN {quote_table(target.source.table, dialect)} "
            f"AS {quote_ident(target.name, dialect)} ON {join.on}"
        )
    # A declared population bound rides EVERY query against its entity — the
    # same mechanism metric filters use: prose stays explanation, the filter
    # does the enforcing.
    population_filters = [
        e.population_filter for e in joined_entities if (e.population_filter or "").strip()
    ]
    # Three sources feed the WHERE (intent filters, definition predicates,
    # shared metric filters) and the model routinely restates a metric's own
    # filter in `filters`, so the same predicate lands ANDed twice. Dedup on
    # normalized text: dropping an exact duplicate can never change semantics.
    predicates: list[str] = []
    seen_predicates: set[str] = set()
    for pred in (
        [
            _predicate(expr, f, dialect)
            for f, (_o, expr) in zip(intent.filters, filter_exprs, strict=True)
        ]
        + [f"({expr})" for expr in definition_exprs]
        + [f"({f})" for f in shared_metric_filters]
        + [f"({f})" for f in population_filters]
    ):
        key = re.sub(r"\s+", " ", pred.strip().strip("()").strip().lower())
        if key in seen_predicates:
            continue
        seen_predicates.add(key)
        predicates.append(pred)
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    if intent.metrics and group:
        sql += " GROUP BY " + ", ".join(group)
    if intent.order_by:
        parts: list[str] = []
        for o, resolved in zip(intent.order_by, order_exprs, strict=True):
            ref = quote_ident(o.field, dialect) if resolved is None else resolved[1]
            parts.append(f"{ref} {o.dir.upper()}")
        sql += " ORDER BY " + ", ".join(parts)
    if intent.limit:
        sql += f" LIMIT {int(intent.limit)}"

    try:
        # `read=` matters now that we emit the dialect's own delimiter: backticks
        # are MySQL/BigQuery quoting and syntax elsewhere, so parsing the string we
        # just built in any other grammar reads our own identifiers wrong.
        return sqlglot.transpile(sql, read=dialect, write=dialect)[0]
    except Exception as exc:  # noqa: BLE001 — any parse/transpile failure → fall back
        raise CompileError(f"could not transpile to {dialect}: {exc}") from exc
