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
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def metric_sql(
    metric: Metric,
    entity: Entity,
    *,
    inline_filters: bool = True,
    where: Sequence[str] = (),
) -> str:
    """The full SQL aggregate expression for a metric, expanded against its entity.

    Simple: ``AGG(expr)``, filters inlined as CASE WHEN when ``inline_filters``
    (used when selected metrics carry DIFFERENT filters, where a shared WHERE
    would intersect them — completed AND returned = zero rows, silently).
    Ratio/derived expand each referenced sibling metric recursively; references
    ALWAYS inline their filters (a shared WHERE would poison the other operand),
    and a ratio wraps as CAST(num AS DOUBLE) / NULLIF(denom, 0); a ratio with
    ``period_weight`` expands to that plain shape too (the weighted form needs
    the intent's window — compile_intent emits it). Unknown
    references and reference cycles raise CompileError. ``where`` are extra
    conditions this one metric is restricted to (its own time window, when
    the selected metrics read different time fields); they ride inside the
    aggregate exactly like its filters.
    """
    return _expand(metric, {m.name: m for m in entity.metrics}, list(where), (), inline_filters)


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
        # A period-weighted ratio expands here as the plain ratio of its
        # operands: this is the SHAPE lints and the filter guard recognise
        # a metric by, never the served SQL — compile_intent routes the
        # metric through _weighted_ratio_sql, where the window gives the
        # weight and the snapshot pin. (Raising here rejected the lens at
        # apply, from the parse lint that only wanted the shape.)
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
    return f"{field}_{grain}", _trunc_sql(entity.name, field, grain, dialect)


def _trunc_sql(table: str, field: str, grain: str, dialect: str) -> str:
    """``table.field`` truncated to *grain*, in the dialect's own DATE_TRUNC form."""
    # Quoting is per-identifier (the table may need it while the field does not),
    # so build the column from two identifiers rather than one `quoted=` flag.
    column = exp.column(
        exp.to_identifier(field, quoted=is_reserved(field, dialect)),
        table=exp.to_identifier(table, quoted=is_reserved(table, dialect)),
    )
    node = exp.DateTrunc(this=column, unit=exp.Literal.string(grain))  # type: ignore[no-untyped-call]
    return node.sql(dialect=dialect)


def level_time_field(entity: Entity, metrics: Sequence[Metric]) -> str | None:
    """The snapshot field a level reading restricts, or None when no selected
    metric is a level. A level beside a flow is refused: one WHERE cannot both
    keep every row of the window and keep only its last snapshot."""
    levels = [m for m in metrics if m.additivity == "level"]
    if not levels:
        return None
    if len(levels) != len(metrics):
        flows = ", ".join(sorted(m.name for m in metrics if m.additivity != "level"))
        raise CompileError(
            f"metrics disagree about additivity: {', '.join(sorted(m.name for m in levels))} "
            f"read as the latest snapshot and {flows} as a sum over the window — ask for "
            "them separately"
        )
    effective = {m.agg_time_field or entity.default_time_field for m in levels}
    if len(effective) > 1:
        named = ", ".join(sorted(f or "(none)" for f in effective))
        raise CompileError(
            f"level metrics disagree about which time field holds the snapshot ({named}) "
            "— ask for them separately"
        )
    field = next(iter(effective))
    if not field:
        raise CompileError(
            f"entity '{entity.name}' declares no default_time_field, so a level metric "
            "cannot find its latest snapshot — add `default_time_field:` to the entity, "
            "or `agg_time_field:` to the metric"
        )
    if not any(f.name == field for f in entity.fields):
        raise CompileError(
            f"entity '{entity.name}' names '{field}' as its time field, but has no such "
            "field — fix default_time_field/agg_time_field"
        )
    return field


def _latest_snapshot(
    entity: Entity,
    field: str,
    window: list[IntentFilter],
    grain: str | None,
    dialect: str,
    zone: str,
    buckets: Sequence[str] = (),
) -> str:
    """The predicate that makes a level metric read as a snapshot: the time
    field equals the latest value inside the window (or inside each grain
    bucket, so a monthly trend is each month's last snapshot). A correlated
    scalar subquery over the same table, which every supported dialect runs;
    the window predicates ride into it so "end of August" is August's last
    snapshot and not the table's.

    *buckets* are the entity's date-typed dimensions the query groups by (a
    ``month`` column on a month-end table): each is a bucket like a grain,
    and the subquery is correlated on it, so grouping by the entity's own
    month column reads one snapshot per month and not the series' last day.

    Only the window restricts the subquery: a snapshot is of the whole
    population at one date, so a dimension or a metric filter aggregates rows
    AT that date rather than moving the date per group — a group that vanished
    before the snapshot reads as absent, which is what its stock was."""
    inner = f"{entity.name}_latest"
    col = f"{quote_ident(inner, dialect)}.{quote_ident(field, dialect)}"
    conds = [_predicate(col, f, dialect, zone) for f in window]
    if grain:
        same_bucket = _trunc_sql(entity.name, field, grain, dialect)
        conds.append(f"{_trunc_sql(inner, field, grain, dialect)} = {same_bucket}")
    for expr in buckets:
        conds.append(
            f"{_realias(expr, entity.name, inner, dialect)} = "
            f"{_realias(expr, entity.name, entity.name, dialect)}"
        )
    table = quote_table(entity.source.table, dialect)
    sub = f"SELECT MAX({col}) FROM {table} AS {quote_ident(inner, dialect)}"
    if conds:
        sub += " WHERE " + " AND ".join(conds)
    return f"{_qualify(entity, field, dialect)} = ({sub})"


def _realias(expr: str, entity: str, alias: str, dialect: str) -> str:
    """*expr* with every column of *entity* (qualified by its name, or bare)
    read from *alias* instead — the same authored expression on both sides of
    the snapshot subquery's correlation."""
    try:
        tree = sqlglot.parse_one(expr, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise CompileError(f"could not parse dimension expression {expr!r}: {exc}") from exc
    for col in tree.find_all(exp.Column):
        if not col.table or col.table == entity:
            col.set("table", exp.to_identifier(alias, quoted=is_reserved(alias, dialect)))
    return tree.sql(dialect=dialect)


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


_PREDICATES = (
    exp.Predicate,  # comparisons, IN, IS, LIKE, SIMILAR TO, GLOB, BETWEEN, EXISTS
    exp.Connector,  # AND / OR
    exp.Not,
    exp.Boolean,
    # Regular-expression matches parse as functions, not predicates: `~`,
    # `~*`, REGEXP, RLIKE, REGEXP_LIKE / REGEXP_CONTAINS / regexp_matches.
    exp.RegexpLike,
    exp.RegexpILike,
    exp.RegexpFullMatch,
    exp.StartsWith,
    exp.EndsWith,
    exp.Contains,
)


def is_predicate(sql: str, dialect: str | None = None) -> bool:
    """True when ``sql`` is a condition a WHERE clause can hold. A definition's
    ``sql_expr`` may instead be a derivation — a column (`brackets.bracket_name`)
    or a formula — which explains the term but, pasted into WHERE, either fails
    on type or silently filters nothing (`AND (position)` is true for every row)."""
    try:
        node = sqlglot.parse_one(sql, read=dialect or None)
    except sqlglot.errors.ParseError:
        return False
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(node, _PREDICATES)


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
            if not is_predicate(d.sql_expr, model.dialect):
                raise CompileError(
                    f"definition '{term}' is a derivation ({d.sql_expr}), not a condition — "
                    "it cannot apply as a filter"
                )
            return d.sql_expr
    raise CompileError(f"unknown definition '{term}'")


# The dialects whose reading of a zone-qualified string literal is verified
# (tests/test_window_lens_timezone.py runs both): against a zone-aware column
# the offset fixes the instant, against a zone-less one it is ignored and the
# wall clock compares as before, and neither depends on the session's time
# zone. How BigQuery, Snowflake and MySQL read an offset against their
# zone-less timestamp types is not verified, so the bound stays a plain
# literal there.
_ZONED_DIALECTS = frozenset({"duckdb", "postgres"})
_NAIVE_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")


def _member_type(entity: Entity, name: str) -> str | None:
    member = name.rpartition(".")[2]
    for f in entity.fields:
        if f.name == member:
            return f.type
    for d in entity.dimensions:
        if d.name == member:
            return d.type
    return None


def _in_zone(value: Any, zone: str) -> Any:
    """A date or zone-less moment as that moment in ``zone``, its offset written
    out ('2026-08-01' in UTC -> '2026-08-01 00:00:00+00:00'); any other value
    unchanged. A plain '2026-08-01' against a timestamp-with-time-zone column
    is read in the warehouse session's zone, so the same window counted
    different rows on servers in different zones."""
    if not isinstance(value, str) or not _NAIVE_MOMENT.match(value):
        return value
    try:
        moment = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise CompileError(f"cannot place {value!r} in the lens timezone {zone!r}: {exc}") from exc
    return moment.isoformat(sep=" ")


def _predicate(col: str, f: IntentFilter, dialect: str, zone: str = "") -> str:
    """``zone``: the lens timezone when ``col`` is a timestamp whose bounds are
    placed in it; empty otherwise."""

    def lit(v: Any) -> str:
        return _lit(_in_zone(v, zone) if zone else v)

    if f.op == "in":
        vals = f.value if isinstance(f.value, list) else [f.value]
        return f"{col} IN ({', '.join(lit(v) for v in vals)})"
    if f.op == "like":
        return f"{col} LIKE {_lit(f.value)}"
    if f.op in _COMPARISON:
        return f"{col} {f.op} {lit(f.value)}"
    raise CompileError(f"unsupported operator '{f.op}'")


def _effective_time_field(entity: Entity, metric: Metric) -> str | None:
    return metric.agg_time_field or entity.default_time_field


def _per_metric_windows(
    intent: QueryIntent, entity: Entity, metrics: Sequence[Metric]
) -> tuple[dict[str, str], list[IntentFilter]]:
    """``({metric: its time field}, window filters)`` when the selected metrics
    read DIFFERENT time fields and the intent bounds one of them — "invoiced
    vs collected in September" is a window on each metric's own date, not one
    date for both. One shared WHERE on either field counts the other metric
    over the wrong period (invoices collected in September instead of invoices
    raised in it), silently. The window filters are lifted out of the WHERE
    and re-placed inside each aggregate on that metric's own field. Empty when
    the metrics agree, or no filter bounds a time field."""
    fields = {m.name: _effective_time_field(entity, m) for m in metrics}
    if len(set(fields.values())) < 2:
        return {}, []
    bounded = {f.field.rpartition(".")[2] for f in intent.filters} & {
        f for f in fields.values() if f
    }
    if not bounded:
        return {}, []
    missing = sorted(m for m, f in fields.items() if not f)
    if missing:
        raise CompileError(
            f"metrics read different time fields, and {', '.join(missing)} has none to "
            "place the window on — add `agg_time_field:` to the metric or "
            "`default_time_field:` to the entity"
        )
    for name, field in fields.items():
        assert field is not None
        if not any(f.name == field for f in entity.fields):
            raise CompileError(
                f"metric '{name}' names '{field}' as its time field, but entity "
                f"'{entity.name}' has no such field — fix default_time_field/agg_time_field"
            )
    window = [
        f
        for f in intent.filters
        if f.field.rpartition(".")[2] in bounded
        and (f.field.partition(".")[0] == entity.name or "." not in f.field)
    ]
    return {m: f for m, f in fields.items() if f}, window


def _governed_columns(metric: Metric, by_name: dict[str, Metric], dialect: str) -> set[str]:
    """The bare column names a metric's meaning already conditions on: every
    column in its ``filters`` and in the WHEN conditions of its ``expr``, and
    the same for the sibling metrics a ratio or derived metric expands."""
    cols: set[str] = set()
    for frag in metric.filters:
        try:
            cols |= {
                c.name.lower() for c in sqlglot.parse_one(frag, read=dialect).find_all(exp.Column)
            }
        except Exception:  # noqa: BLE001 — an unparseable fragment governs nothing we can name
            pass
    if metric.expr and metric.type == "simple":
        try:
            tree = sqlglot.parse_one(metric.expr, read=dialect)
        except Exception:  # noqa: BLE001
            tree = None
        if tree is not None:
            for when in tree.find_all(exp.If):
                cols |= {c.name.lower() for c in when.this.find_all(exp.Column)}
    refs = [metric.numerator, metric.denominator] if metric.type == "ratio" else []
    if metric.type == "derived" and metric.expr:
        refs = _PLACEHOLDER.findall(metric.expr)
    for ref in refs:
        sibling = by_name.get(ref or "")
        if sibling is not None and sibling is not metric:
            cols |= _governed_columns(sibling, by_name, dialect)
    return cols


def prefer_weighted_ratio(intent: QueryIntent, model: SemanticModel) -> QueryIntent:
    """The intent with a level and a flow replaced by the period-weighted
    ratio metric declared over exactly those two — "utilisation" asked as
    scheduled appointments and slot capacity. The model names the operands
    the question spells out; the compiler refuses a level beside a flow;
    the author declared how they meet. Deterministic, before that refusal."""
    if len(intent.metrics) != 2:
        return intent
    try:
        entity = _resolve_entity(model, intent.entity, intent.metrics)
    except CompileError:
        return intent
    asked = set(intent.metrics)
    for m in entity.metrics:
        if m.type == "ratio" and m.period_weight and {m.numerator, m.denominator} == asked:
            return intent.model_copy(update={"metrics": [m.name]})
    return intent


def govern_literals(intent: QueryIntent, model: SemanticModel) -> QueryIntent:
    """The intent with every equality/IN literal dropped whose column a
    selected metric already governs, each recorded in ``literals_dropped``.

    "completed visits" compiled to ``status = 'completed'`` beside the metric's
    own ``status IN ('completed', 'billed')``: the question word became a second
    predicate on the governed column, and a count halved in silence. The
    metric's predicate IS the definition of the figure, so a literal on the
    same column is not a narrowing the caller chose but the generator restating
    the metric in its own words — it is dropped, and the ledger says so
    (``literal_dropped``), never silently either way. Columns are matched by
    bare name on the base entity; a literal qualified with another entity is
    left alone (it filters a joined dimension, which a metric never governs).
    Comparison operators are kept: ``amount > 100`` on a filtered column is a
    different kind of ask."""
    if not intent.metrics or not intent.filters:
        return intent
    try:
        entity = _resolve_entity(model, intent.entity, intent.metrics)
    except CompileError:
        return intent
    by_name = {m.name: m for m in entity.metrics}
    governed: dict[str, list[str]] = {}
    for name in intent.metrics:
        metric = by_name.get(name)
        if metric is None:
            continue
        for col in _governed_columns(metric, by_name, model.dialect):
            governed.setdefault(col, []).append(name)
    if not governed:
        return intent
    kept: list[IntentFilter] = []
    dropped: list[str] = []
    for f in intent.filters:
        owner, _, col = f.field.rpartition(".")
        if f.op in ("=", "in") and (not owner or owner == entity.name) and col.lower() in governed:
            value = f.value if isinstance(f.value, list) else [f.value]
            literal = f"{col} {'IN' if f.op == 'in' else '='} {', '.join(_lit(v) for v in value)}"
            dropped.append(f"{literal} (governed by {', '.join(governed[col.lower()])})")
            continue
        kept.append(f)
    if not dropped:
        return intent
    return intent.model_copy(
        update={"filters": kept, "literals_dropped": [*intent.literals_dropped, *dropped]}
    )


def _window_days(window: Sequence[IntentFilter], field: str) -> int:
    """The number of days the intent's bounds on *field* span, inclusive of
    both ends — the fact a period weight is multiplied from. Refuses an open
    window: "weeks in the period" needs a period."""
    lo: date | None = None
    hi: date | None = None
    for f in window:
        value = _as_day(f.value)
        if value is None:
            continue
        if f.op in (">=", "="):
            lo = value if lo is None else max(lo, value)
        elif f.op == ">":
            lo = value + timedelta(days=1) if lo is None else max(lo, value + timedelta(days=1))
        if f.op in ("<=", "="):
            hi = value if hi is None else min(hi, value)
        elif f.op == "<":
            hi = value - timedelta(days=1) if hi is None else min(hi, value - timedelta(days=1))
    if lo is None or hi is None or hi < lo:
        raise CompileError(
            f"a period-weighted ratio needs a window bounded on both ends on '{field}' "
            "(the weight is the period's length) — ask for a period, e.g. 'in September 2026'"
        )
    return (hi - lo).days + 1


def _as_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
    return None


def _weighted_ratio_sql(
    metric: Metric,
    entity: Entity,
    intent: QueryIntent,
    filter_owners: Sequence[Entity],
    dialect: str,
    zone: str,
) -> str:
    """A ratio across additivities, compiled as the handbook writes it: the
    flow operand summed over the window, the level operand read at the
    window's latest snapshot and multiplied by the declared period weight
    (``weeks`` = days / 7, ``days``). Utilisation = scheduled appointments /
    (slots per week x weeks in the month). The snapshot pin is the same
    correlated subquery a level metric reads through on its own, and the
    weight is a number computed here from the intent's bounds, so the served
    SQL shows it — nothing is left for a reader to reconstruct."""
    assert metric.numerator is not None and metric.denominator is not None
    by_name = {m.name: m for m in entity.metrics}
    num = _ref(metric.numerator, by_name, metric.name)
    den = _ref(metric.denominator, by_name, metric.name)
    levels = [m for m in (num, den) if m.additivity == "level"]
    if len(levels) != 1:
        raise CompileError(
            f"metric '{metric.name}' is period-weighted, so exactly one of its operands "
            "must be a level (additivity: level) and the other a flow — "
            f"{num.name} and {den.name} are "
            + ("both levels" if len(levels) == 2 else "both flows")
        )
    level = levels[0]
    if intent.grain:
        raise CompileError(
            f"metric '{metric.name}' is period-weighted and reads one window at a time — "
            "ask for one period, not a trend"
        )
    field = level.agg_time_field or entity.default_time_field
    if not field or not any(f.name == field for f in entity.fields):
        raise CompileError(
            f"metric '{metric.name}' weights level '{level.name}' by the period, which "
            "needs a time field — add `default_time_field:` to the entity or "
            "`agg_time_field:` to the metric"
        )
    window = [
        f
        for f, owner in zip(intent.filters, filter_owners, strict=True)
        if owner is entity and f.field.rpartition(".")[2] == field
    ]
    days = _window_days(window, field)
    weight = f"({days} / 7.0)" if metric.period_weight == "weeks" else str(days)
    # The pin's literals are zoned only for a timestamp field, as the WHERE's are.
    pin_zone = zone if _member_type(entity, field) == "timestamp" else ""
    pin = _latest_snapshot(entity, field, window, None, dialect, pin_zone)
    assert level.agg is not None and level.expr is not None
    conds = " AND ".join([pin, *metric.filters, *level.filters])
    level_sql = f"{_AGG[level.agg]}(CASE WHEN {conds} THEN {level.expr} END) * {weight}"
    flow = num if level is den else den
    flow_sql = _expand(flow, by_name, list(metric.filters), (metric.name,), True)
    if level is den:
        return f"CAST({flow_sql} AS DOUBLE) / NULLIF({level_sql}, 0)"
    return f"CAST({level_sql} AS DOUBLE) / NULLIF({flow_sql}, 0)"


def compile_intent(intent: QueryIntent, model: SemanticModel) -> str:
    # Before resolving anything: an empty intent has no entity to be ambiguous
    # about, and "ambiguous entity" is a confusing way to say "you asked for nothing".
    if not intent.metrics and not intent.dimensions and not intent.fields:
        raise CompileError("intent selects neither a metric nor a dimension")
    if intent.fields and intent.metrics:
        raise CompileError("a listing (fields) and an aggregate (metrics) cannot mix")

    intent = govern_literals(prefer_weighted_ratio(intent, model), model)
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
    # Metrics on different time fields each take the window inside their own
    # aggregate (see _per_metric_windows), which is the CASE form as well.
    window_fields, window_filters = _per_metric_windows(
        intent, entity, [metrics_by[m] for m in intent.metrics]
    )
    mixed = len(filter_sets) > 1 or compound or bool(window_fields)
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
    field_exprs: list[tuple[Entity, str]] = [
        _resolve_member(model, entity, f, dialect) for f in intent.fields
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
    selected_aliases = (
        set(metrics_by)
        | set(intent.dimensions)
        | set(intent.fields)
        | ({bucket[0]} if bucket else set())
    )
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
    for f, (_owner, expr) in zip(intent.fields, field_exprs, strict=True):
        # A listing projects members row by row: no aggregation, no GROUP BY.
        select.append(f"{expr} AS {quote_ident(f, dialect)}")
    # A bound on a timestamp is a moment in the lens's declared clock; an
    # undeclared clock is never invented, and the literal then stays plain.
    zone = model.timezone if dialect in _ZONED_DIALECTS else ""
    for m in intent.metrics:
        own_window = [
            _predicate(
                _qualify(entity, window_fields[m], dialect),
                f,
                dialect,
                zone if _member_type(entity, window_fields[m]) == "timestamp" else "",
            )
            for f in window_filters
            if m in window_fields
        ]
        if metrics_by[m].type == "ratio" and metrics_by[m].period_weight:
            weighted = _weighted_ratio_sql(
                metrics_by[m], entity, intent, [o for o, _ in filter_exprs], dialect, zone
            )
            select.append(f"{weighted} AS {quote_ident(m, dialect)}")
            continue
        select.append(
            f"{metric_sql(metrics_by[m], entity, inline_filters=mixed, where=own_window)} "
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
        [o for o, _ in (*dim_exprs, *field_exprs, *filter_exprs, *[x for x in order_exprs if x])]
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
    lifted = {id(f) for f in window_filters}
    for pred in (
        [
            _predicate(
                expr, f, dialect, zone if _member_type(owner, f.field) == "timestamp" else ""
            )
            for f, (owner, expr) in zip(intent.filters, filter_exprs, strict=True)
            if id(f) not in lifted
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
    # A level metric (ARR, headcount, a balance) is a snapshot: its figure for a
    # window is the LAST snapshot inside it, never SUM over every snapshot row.
    # Without this a quarter's "total ARR" summed three month-ends into one
    # number three times too large — a silent wrong answer the aggregate alone
    # cannot see, because the rows and the SQL are both well-formed.
    snapshot_field = level_time_field(entity, [metrics_by[m] for m in intent.metrics])
    if snapshot_field is not None:
        window = [
            f
            for f, (owner, _expr) in zip(intent.filters, filter_exprs, strict=True)
            if owner is entity and f.field.rpartition(".")[2] == snapshot_field
        ]
        buckets = [
            expr
            for d, (owner, expr) in zip(intent.dimensions, dim_exprs, strict=True)
            if owner is entity and _member_type(entity, d) in ("date", "timestamp")
        ]
        predicates.append(
            _latest_snapshot(
                entity,
                snapshot_field,
                window,
                intent.grain,
                dialect,
                zone if _member_type(entity, snapshot_field) == "timestamp" else "",
                buckets,
            )
        )
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
