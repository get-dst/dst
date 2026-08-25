"""filter_guard — deterministic completeness check for governed metric filters.

A metric's ``filters`` are part of its MEANING (bookings = new-customer deals
only). The intent path compiles them in deterministically; the raw-SQL path only
ASKS the model to include them (the prompt's "REQUIRES filters" line) — and a
serving model can ignore that line while the guard and execution wave the
unfiltered SQL through. The disease is a governance guarantee living in a
prompt. This module puts it in code: detect,
via sqlglot, when SQL computes a filtered metric's aggregation without its
filter, so the pipeline can repair or reject before the warehouse is touched.

Bias, by explicit contract: UNDER-detection is acceptable (a metric computed in
a form we can't recognize goes unchecked); FALSE POSITIVES ARE NOT (a correctly
filtered query must never be blocked). Every heuristic leans that way —
aggregation matching is strict (every column must provably resolve to the
metric's entity), filter satisfaction is generous (any WHERE / HAVING /
QUALIFY / join-ON conjunct anywhere in the statement, a CASE WHEN or IF guard
wrapping the aggregation, a FILTER (WHERE ...) clause, and boolean idioms
``x`` / ``x = TRUE`` are unified). Known unchecked forms: a ratio/derived
metric's OWN filters (its referenced simple metrics are still checked wherever
their aggregations appear), aggregations whose columns can't be attributed to
one entity (multi-table scope with unqualified columns), and multi-branch CASE.

Attribution is by RESOLUTION: an aggregation's columns resolve to an
entity, and the aggregation is matched against ALL of that entity's simple
metrics sharing its canonical form. The twin rule follows: a bare aggregation
violates only when EVERY matching metric requires filters the statement does
not satisfy — an unfiltered twin (session_count next to converted_sessions,
both COUNT(session_id)) legitimizes the bare form, as does a filtered twin
whose every filter is satisfied. The obvious false positive —
"how many sessions" attributed to the filtered twin — dies here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.simplify import simplify

# Table-name matching reuses the certified-bindings idiom: a bare name denotes a
# qualified one's last segment, two different qualifications never match.
from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import Entity, Metric, SemanticModel
from services.runtime.compiler import metric_sql

_CONDITION_HOMES = (exp.Where, exp.Having, exp.Qualify)

# (aggregation node type, DISTINCT?, canonical inner expression) — the identity
# an aggregation is matched by; every simple metric of an entity maps to one.
_AggKey = tuple[type[exp.Expression], bool, str]


def _cleared(
    frag_conjs: list[tuple[str, list[str]]], satisfied: set[str], today: date | None = None
) -> bool:
    """Could the statement legitimately be computing this metric here? True when
    every checkable filter conjunct is satisfied — vacuously true for an
    unfiltered metric (frag_conjs empty), the heart of the twin rule."""
    return all(
        c in satisfied or _bound_implied(c, satisfied, today)
        for _frag, conjs in frag_conjs
        for c in conjs
    )


# ── bound implication ────────────────────────────────────────────────────────
# A required filter of the form `col <= CURRENT_DATE` demanding its own TEXT
# among the conjuncts rejects `ordered_at = DATE_SUB(CURRENT_DATE, INTERVAL
# '1' DAY)` — yesterday, which cannot be after today — as unfiltered, and that
# shape is common. The general implication problem is not worth solving; the
# narrow case that covers the real failures is:
# when the REQUIRED conjunct is an inequality bound on a column, accept any
# satisfied predicate on that column whose range provably lies inside the
# bound. "Provably" is conservative date arithmetic — day/week exact,
# month/quarter/year on the safe side — and anything not estimable is never
# accepted. Equality/IN-style required filters keep the exact match (there is
# no "narrower" to reason about).

_DAYS_SAFE = {  # unit -> (min_days, max_days): the two safe sides
    "day": (1, 1),
    "week": (7, 7),
    "month": (28, 31),
    "quarter": (90, 92),
    "year": (365, 366),
}


def _date_estimate(node: exp.Expression, today: date, *, upper: bool) -> date | None:
    """A conservative date for a comparison side: exact for ISO literals and
    CURRENT_DATE; CURRENT_DATE ± INTERVAL uses the safe side of the unit; None
    for anything else (never accept on a guess)."""
    if isinstance(node, exp.Paren | exp.Cast):
        return _date_estimate(node.this, today, upper=upper)
    if isinstance(node, exp.Literal) and node.is_string:
        try:
            return date.fromisoformat(node.this[:10])
        except ValueError:
            return None
    if isinstance(node, exp.CurrentDate):
        return today
    if isinstance(node, exp.DateSub | exp.DateAdd):
        base = _date_estimate(node.this, today, upper=upper)
        interval_n = node.expression
        unit = (node.text("unit") or "day").lower()
        if base is None or not isinstance(interval_n, exp.Literal):
            return None
        try:
            n = int(str(interval_n.this))
        except ValueError:
            return None
        lo, hi = _DAYS_SAFE.get(unit, (None, None))
        if lo is None or hi is None:
            return None
        if isinstance(node, exp.DateSub):
            # Subtracting: the LATER (upper) estimate subtracts the FEWEST days.
            return base - timedelta(days=n * (lo if upper else hi))
        return base + timedelta(days=n * (hi if upper else lo))
    return None


def _column_of(node: exp.Expression) -> str | None:
    return node.name.lower() if isinstance(node, exp.Column) and node.name else None


def _range_of(
    cond: exp.Expression, column: str, today: date
) -> tuple[date | None, date | None] | None:
    """The (lower, upper) date range a satisfied conjunct pins *column* into,
    conservative on both ends. None = the conjunct says nothing provable."""
    if isinstance(cond, exp.Paren):
        return _range_of(cast(exp.Expression, cond.this), column, today)
    if isinstance(cond, exp.And):
        # `_canon` runs simplify, which rewrites BETWEEN into an AND pair —
        # intersect the children's ranges.
        lo: date | None = None
        hi: date | None = None
        for term in cond.flatten():
            rng = _range_of(cast(exp.Expression, term), column, today)
            if rng is None:
                continue
            t_lo, t_hi = rng
            if t_lo is not None:
                lo = t_lo if lo is None else max(lo, t_lo)
            if t_hi is not None:
                hi = t_hi if hi is None else min(hi, t_hi)
        return (lo, hi) if lo is not None or hi is not None else None
    if isinstance(cond, exp.Between) and _column_of(cond.this) == column:
        lo = _date_estimate(cond.args["low"], today, upper=False)
        hi = _date_estimate(cond.args["high"], today, upper=True)
        return (lo, hi)
    if isinstance(cond, exp.EQ | exp.LTE | exp.LT | exp.GTE | exp.GT):
        col, rhs = cond.this, cond.expression
        if _column_of(col) != column:
            return None
        if isinstance(cond, exp.EQ):
            return (
                _date_estimate(rhs, today, upper=False),
                _date_estimate(rhs, today, upper=True),
            )
        if isinstance(cond, exp.LTE | exp.LT):
            hi = _date_estimate(rhs, today, upper=True)
            if hi is not None and isinstance(cond, exp.LT):
                hi = hi - timedelta(days=1)
            return (None, hi)
        lo = _date_estimate(rhs, today, upper=False)
        if lo is not None and isinstance(cond, exp.GT):
            lo = lo + timedelta(days=1)
        return (lo, None)
    return None


def _bound_implied(required: str, satisfied: set[str], today: date | None) -> bool:
    """Does any satisfied conjunct provably imply the required inequality bound?"""
    if today is None:
        return False
    try:
        req = sqlglot.parse_one(required)
    except Exception:
        return False
    if not isinstance(req, exp.LTE | exp.LT | exp.GTE | exp.GT):
        return False
    column = _column_of(req.this)
    if column is None:
        return False
    upper_bound = isinstance(req, exp.LTE | exp.LT)
    bound = _date_estimate(req.expression, today, upper=not upper_bound)
    if bound is not None and isinstance(req, exp.LT):
        bound = bound - timedelta(days=1)
    if bound is not None and isinstance(req, exp.GT):
        bound = bound + timedelta(days=1)
    if bound is None:
        return False
    for cond_text in satisfied:
        try:
            cond = sqlglot.parse_one(cond_text)
        except Exception:
            continue
        rng = _range_of(cast(exp.Expression, cond), column, today)
        if rng is None:
            continue
        lo, hi = rng
        if upper_bound and hi is not None and hi <= bound:
            return True
        if not upper_bound and lo is not None and lo >= bound:
            return True
    return False


def _alias_map(stmt: exp.Expression) -> dict[str, set[str]]:
    """alias/name (lowercased) -> physical table refs it may denote. CTE names
    shadow only unqualified references (the sql_guard/bindings rule); an alias
    reused for two different tables keeps BOTH candidates — strict matching then
    refuses it, lenient matching accepts any."""
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    out: dict[str, set[str]] = {}
    for tbl in stmt.find_all(exp.Table):
        if not tbl.name:
            continue
        if not (tbl.db or tbl.catalog) and tbl.name.lower() in cte_names:
            continue
        full = ".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p)
        out.setdefault(tbl.name.lower(), set()).add(full)
        if tbl.alias:
            out.setdefault(tbl.alias.lower(), set()).add(full)
    return out


def _conjuncts(cond: exp.Expression) -> list[exp.Expression]:
    """Flatten a condition into its ANDed conjuncts, parens unwrapped."""
    node = cond
    while isinstance(node, exp.Paren):
        node = node.this
    parts = list(node.flatten()) if isinstance(node, exp.And) else [node]
    out: list[exp.Expression] = []
    for part in parts:
        while isinstance(part, exp.Paren):
            part = part.this
        if isinstance(part, exp.Expression):
            out.append(part)
    return out


def _bool_norm(node: exp.Expression) -> exp.Expression:
    """Unify equivalent condition idioms so a filter authored one way is
    recognized written another (blocking a correctly-filtered query is the one
    failure mode this module must never have): ``x = TRUE`` / ``x IS TRUE`` ->
    ``x``, the FALSE forms -> ``NOT x``, single-element ``x IN (v)`` ->
    ``x = v`` (a common LLM emission)."""
    if (
        isinstance(node, exp.In)
        and len(node.expressions) == 1
        and not node.args.get("query")
        and not node.args.get("unnest")
    ):
        node = exp.EQ(this=node.this, expression=node.expressions[0])
    if isinstance(node, exp.EQ | exp.Is) and isinstance(node.expression, exp.Boolean):
        return node.this if node.expression.this else exp.Not(this=node.this)
    return node


# The latest-snapshot predicate, in every spelling the generator writes it.
# Both generation prompts now steer 'now'/'current' on a status-date column to
# `col = (SELECT MAX(col) FROM …)` rather than today's literal (snapshots lag).
# A metric governed by exactly that filter was then rejected as UNFILTERED
# whenever the model aliased the inner table or hoisted the MAX into a CTE —
# the same predicate, spelled differently, canonicalizes differently. That is
# the false positive this module must never have: a correctly-filtered query
# refused. So the shape normalizes to one form before comparison.
_LATEST = "__latest__"


def _snapshot_select(node: exp.Expression) -> exp.Select | None:
    """The SELECT behind a scalar-subquery operand, unwrapped."""
    while isinstance(node, exp.Paren | exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _max_of(select: exp.Select, ctes: dict[str, exp.Expression]) -> tuple[str, str] | None:
    """``(column, source table)`` when *select* is exactly ``SELECT MAX(col)
    FROM <table>`` — following one CTE hop when the projection is that CTE's
    column. Any narrowing clause (WHERE/GROUP/HAVING/JOIN/DISTINCT/LIMIT) means
    a different value than the metric declares, so it is never accepted."""
    if len(select.expressions) != 1:
        return None
    if any(select.args.get(k) for k in ("where", "group", "having", "joins", "distinct", "limit")):
        return None
    src = select.args.get("from_") or select.args.get("from")  # key renamed across sqlglot
    table = src.this if src is not None else None
    if not isinstance(table, exp.Table) or not table.name:
        return None
    proj = select.expressions[0]
    if isinstance(proj, exp.Alias):
        proj = proj.this
    if isinstance(proj, exp.Max):
        col = proj.this
        return (col.name.lower(), table.name.lower()) if isinstance(col, exp.Column) else None
    # One CTE hop: `SELECT max_date FROM latest`, where `latest` is the MAX.
    if isinstance(proj, exp.Column) and (inner := ctes.get(table.name.lower())) is not None:
        inner_select = _snapshot_select(inner)
        return _max_of(inner_select, {}) if inner_select is not None else None
    return None


def _snapshot_norm(
    node: exp.Expression,
    ctes: dict[str, exp.Expression],
    table_ok: Callable[[str], bool],
) -> exp.Expression:
    """``col = (SELECT MAX(col) FROM t)`` -> ``col = __latest__(col)``, so the
    declared fragment and the generated predicate compare equal however the
    subquery was written. Untouched unless the max is over the SAME column on a
    table that denotes the entity — a max over anything else is a different
    value, and crediting it would be the over-trust this guard exists to
    prevent."""
    if not isinstance(node, exp.EQ):
        return node
    for col_side, sub_side in ((node.this, node.expression), (node.expression, node.this)):
        if not isinstance(col_side, exp.Column):
            continue
        select = _snapshot_select(sub_side)
        if select is None:
            continue
        found = _max_of(select, ctes)
        if found is None or found[0] != col_side.name.lower() or not table_ok(found[1]):
            continue
        return exp.EQ(
            this=col_side.copy(),
            expression=exp.Anonymous(this=_LATEST, expressions=[col_side.copy()]),
        )
    return node


def _canon(
    node: exp.Expression,
    entity: Entity,
    alias_map: dict[str, set[str]],
    sole_source: bool,
    *,
    strict: bool,
    condition: bool = False,
    ctes: dict[str, exp.Expression] | None = None,
) -> str | None:
    """Canonical comparison string for an expression, or None when its columns
    can't be attributed to *entity*. Column qualifiers must denote the entity
    (by its name — the compiler's alias — or its source table, bare-or-qualified)
    and are then stripped; identifiers are lowercased and unquoted; ``simplify``
    normalizes literals/parens. ``strict`` (aggregation matching) demands every
    resolution candidate match and unqualified columns only in single-table
    scope; lenient (filter satisfaction) accepts any match."""
    n = node.copy()
    ent = entity.name.lower()
    src = entity.source.table
    if condition:
        # Before attribution: the normalized form has no subquery left, so the
        # inner spelling (alias, CTE hop) can never decide the outcome.
        n = _snapshot_norm(
            n,
            ctes or {},
            lambda t: (
                t == ent
                or _ref_matches(t, src)
                or any(_ref_matches(r, src) for r in alias_map.get(t, ()))
            ),
        )
    for col in list(n.find_all(exp.Column)):
        if col.db or col.catalog:
            refs = {".".join(p.lower() for p in (col.catalog, col.db, col.table) if p)}
        elif col.table:
            refs = alias_map.get(col.table.lower(), {col.table.lower()})
        else:
            refs = set()
        if refs:
            hits = [r == ent or _ref_matches(r, src) for r in refs]
            ok = all(hits) if strict else any(hits)
        else:
            ok = sole_source if strict else True
        if not ok:
            return None
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    try:
        n = simplify(n)
    except Exception:  # noqa: BLE001 — simplify is best-effort canonicalization
        pass
    if condition:
        n = _bool_norm(n)
    for ident in n.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
        ident.set("quoted", False)
    return n.sql()


def _unwrap_guard(arg: exp.Expression) -> tuple[exp.Expression, list[exp.Expression]]:
    """Split an aggregation argument into (inner expression, guarding conditions)
    for the single-branch filter idioms the compiler and dialects use:
    ``CASE WHEN cond THEN expr END`` (no ELSE / ELSE NULL) and
    ``IF(cond, expr, NULL)``. Anything else guards nothing."""
    if isinstance(arg, exp.Case) and not arg.this:  # searched CASE, no operand
        ifs = arg.args.get("ifs") or []
        default = arg.args.get("default")
        if len(ifs) == 1 and (default is None or isinstance(default, exp.Null)):
            return ifs[0].args["true"], _conjuncts(ifs[0].this)
    if isinstance(arg, exp.If):
        false = arg.args.get("false")
        if false is None or isinstance(false, exp.Null):
            return arg.args["true"], _conjuncts(arg.this)
    return arg, []


def corrected_aggregation(metric_name: str, model: SemanticModel) -> str | None:
    """The exact aggregation SQL a repair should emit for *metric_name* —
    compiler.metric_sql with the governed filters inlined (the CASE-wrapped
    form). Copy-pasteable repair feedback: naming the bare condition is too
    little to steer a repair — the model re-emits its own unfiltered form and
    the next attempt hard-refuses a pattern the repair path can already fix."""
    for entity in model.entities:
        for metric in entity.metrics:
            if metric.name == metric_name:
                try:
                    return metric_sql(metric, entity, inline_filters=True)
                except Exception:  # noqa: BLE001 — a malformed metric is validate's problem
                    return None
    return None


def _question_resolved(
    twins: list[tuple[Metric, list[tuple[str, list[str]]]]], question: str
) -> tuple[Metric, list[tuple[str, list[str]]]] | None:
    """The twin the QUESTION names, when it names exactly one.

    Two metrics over one expression with different mandatory filters is the
    standard snapshot-table pattern (current_* = latest snapshot, total_* =
    end-of-month series). When neither filter is satisfied, demanding the
    union is the one option that can never be satisfied — "current ARR by
    tier" already says which metric it means, and the guard must hear it.
    Strictly-best name-token overlap wins; a tie or zero signal returns None
    (the union stays, and the rejection names the conflict instead of the
    phrasing)."""
    qtokens = set(re.findall(r"[a-z0-9]+", question.lower()))

    def score(m: Metric) -> int:
        return len(set(m.name.lower().split("_")) & qtokens)

    scored = sorted(((score(m), i) for i, (m, _f) in enumerate(twins)), reverse=True)
    best_score, best_i = scored[0]
    if best_score == 0 or (len(scored) > 1 and scored[1][0] == best_score):
        return None
    return twins[best_i]


def conflict_note(unfiltered: list[tuple[str, str]], model: SemanticModel) -> str | None:
    """When the missing list demands the filters of metrics that SHARE one
    canonical aggregation, say so — that rejection is caused by the model's
    shape plus an unresolvable phrasing, not by the phrasing alone, so
    'try rephrasing' only sends the user in circles."""
    names = list(dict.fromkeys(n for n, _f in unfiltered))
    if len(names) < 2:
        return None
    by_agg: dict[tuple[str, str], list[str]] = {}
    for entity in model.entities:
        for metric in entity.metrics:
            if metric.name not in names or metric.type != "simple":
                continue
            try:
                canon = sqlglot.parse_one(
                    metric_sql(metric, entity, inline_filters=False), read=model.dialect
                ).sql(dialect=model.dialect)
            except Exception:  # noqa: BLE001 — a malformed metric is validate's problem
                continue
            by_agg.setdefault((entity.name, canon), []).append(metric.name)
    for (_entity, _agg), twins in by_agg.items():
        if len(twins) >= 2:
            listed = ", ".join(f"'{n}'" for n in twins)
            return (
                f"metrics {listed} govern the same expression with different mandatory "
                f"filters, and the question did not single one out — re-ask naming the "
                f"metric you mean (e.g. '{twins[0]}')"
            )
    return None


def missing_metric_filters(
    sql: str, model: SemanticModel, question: str | None = None
) -> list[tuple[str, str]]:
    """For each entity metric with filters that *sql* provably COMPUTES, report
    the filter fragments the statement does not satisfy — ``[(metric_name,
    filter_fragment), ...]``, empty when the governance contract holds.

    ``question``, when given, resolves filter demands within a group of metrics
    sharing one canonical aggregation: only the metric the question names is
    demanded (see ``_question_resolved``) — the union of incompatible twins is
    permanently unsatisfiable.

    A metric is computed when its canonical aggregation (compiler.metric_sql —
    e.g. ``SUM(deals.contract_value_eur)``) appears as an expression node, the
    aggregated expression matching after canonicalization with every column
    strictly attributed to the metric's entity. A filter is satisfied when each
    of its conjuncts appears among the statement's WHERE/HAVING/QUALIFY/join-ON
    conjuncts, the CASE WHEN / IF guard wrapping that aggregation, or its
    FILTER (WHERE ...) clause."""
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's rejection, not ours
        return []
    if not isinstance(stmt, exp.Expression):
        return []
    from services.runtime.generator import business_today

    today = date.fromisoformat(business_today(model.timezone))
    alias_map = _alias_map(stmt)
    all_refs = set().union(*alias_map.values()) if alias_map else set()

    # CTE bodies, for the one hop `col = (SELECT max_date FROM latest)` takes.
    ctes = {c.alias_or_name.lower(): c.this for c in stmt.find_all(exp.CTE)}

    def lenient(conds: list[exp.Expression], entity: Entity, sole: bool) -> set[str]:
        canon = (
            _canon(c, entity, alias_map, sole, strict=False, condition=True, ctes=ctes)
            for c in conds
        )
        return {s for s in canon if s is not None}

    missing: list[tuple[str, str]] = []
    for entity in model.entities:
        src = entity.source.table
        sole_source = bool(all_refs) and all(_ref_matches(r, src) for r in all_refs)

        # Conditions that count as filtering ANYWHERE in the statement — generous
        # by design: a filter satisfied in a subquery/CTE WHERE or an inner-join ON
        # genuinely filters in the common shapes, and the cost of over-crediting is
        # a missed violation, never a blocked correct query.
        global_conds: set[str] = set()
        for home in _CONDITION_HOMES:
            for clause in stmt.find_all(home):
                global_conds |= lenient(_conjuncts(clause.this), entity, sole_source)
        for join in stmt.find_all(exp.Join):
            on = join.args.get("on")
            if on is not None:
                global_conds |= lenient(_conjuncts(on), entity, sole_source)

        # Attribution by resolution: group ALL of the entity's simple metrics by
        # canonical aggregation. An aggregation node instantiates ONE metric of
        # its group — which one is unknowable from the SQL alone, so the twin
        # rule applies: flag only when every group member requires filters the
        # statement doesn't satisfy.
        groups: dict[_AggKey, list[tuple[Metric, list[tuple[str, list[str]]]]]] = {}
        for metric in entity.metrics:
            if metric.type != "simple":
                continue  # ratio/derived OWN filters: documented under-detection
            try:
                canon_agg = sqlglot.parse_one(
                    metric_sql(metric, entity, inline_filters=False), read=model.dialect
                )
            except Exception:  # noqa: BLE001 — a malformed metric is validate's problem
                continue
            canon_inner_node = canon_agg.this
            distinct = isinstance(canon_inner_node, exp.Distinct)
            if distinct:
                if len(canon_inner_node.expressions) != 1:
                    continue
                canon_inner_node = canon_inner_node.expressions[0]
            canon_inner = _canon(canon_inner_node, entity, {}, True, strict=True)
            if canon_inner is None:
                continue

            # Canonical conjuncts per filter fragment; an unparseable fragment
            # can't be checked (validate-time problem) — never flag on it.
            frag_conjs: list[tuple[str, list[str]]] = []
            for frag in metric.filters:
                try:
                    parsed = sqlglot.parse_one(frag, read=model.dialect)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(parsed, exp.Expression):
                    continue
                canon = [
                    _canon(c, entity, {}, True, strict=True, condition=True)
                    for c in _conjuncts(parsed)
                ]
                if all(c is not None for c in canon):
                    frag_conjs.append((frag, [c for c in canon if c is not None]))
            # sqlglot types parse_one over a TypeVar; pin the concrete node class.
            agg_node_type = cast(type[exp.Expression], type(canon_agg))
            groups.setdefault((agg_node_type, distinct, canon_inner), []).append(
                (metric, frag_conjs)
            )

        for (agg_type, distinct, canon_inner), twins in groups.items():
            if not any(fcs for _m, fcs in twins):
                continue  # no checkable governed filters in this shape
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
                cand = _canon(inner, entity, alias_map, sole_source, strict=True)
                if cand is None or cand != canon_inner:
                    continue  # not (provably) this group — under-detect, never guess
                satisfied = global_conds | lenient(guard_conds, entity, sole_source)
                # The twin rule: any member this aggregation could legitimately
                # be — no filters, no checkable filters, or every fragment
                # satisfied — clears the form for the whole group.
                if any(_cleared(fcs, satisfied, today) for _m, fcs in twins):
                    continue
                # The question resolves twins before the union is demanded:
                # "current ARR" binds current_arr's filters alone; only a tie
                # or zero signal keeps the whole group.
                members = twins
                if question and len(twins) > 1:
                    picked = _question_resolved(twins, question)
                    if picked is not None:
                        members = [picked]
                for metric, fcs in members:
                    for frag, conjs in fcs:
                        hit = (metric.name, frag)
                        if hit not in missing and any(
                            c not in satisfied and not _bound_implied(c, satisfied, today)
                            for c in conjs
                        ):
                            missing.append(hit)
    return missing
