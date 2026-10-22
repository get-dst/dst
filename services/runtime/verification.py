"""Build the graded verification report from named, deterministic checks.

Replaces the binary high/low confidence: each check passes/fails/skips with a reason,
and the headline grade is derived from the breakdown — never the reverse. The checks
target the two real failure classes: invented numbers (numeric_grounding) and
wrong-but-valid SQL (definition_applied, intent_alignment).
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import expressions as exp

from services.contracts.resolution import DecisionRecord
from services.contracts.semantic_model import Definition, Entity, SemanticModel
from services.contracts.verification import (
    CheckStatus,
    Grade,
    VerificationCheck,
    VerificationReport,
)
from services.contracts.warehouse import QueryResult
from services.lenses.profile_age import STALE_REASON
from services.lenses.profile_enrich import EntityCoverage
from services.runtime import adversary, faithfulness, sql_canon, time_guard
from services.runtime.answer import grounding_notes
from services.runtime.canon import canon_cell
from services.runtime.timewindow import (
    breakdown_unit,
    is_day_window,
    month_end_as_month,
    sql_engages_time,
    temporal_terms,
    unplaced_days,
    window_ranges,
    year_parts,
)

_DISTINCT_ASK = re.compile(r"\b(unique|distinct|individual|deduplicated)\b", re.IGNORECASE)
_COUNT_ASK = re.compile(r"\b(how many|count|number of)\b", re.IGNORECASE)


def _norm(sql_fragment: str) -> str:
    # Whitespace-insensitive: a definition's sql_expr is "in" the SQL whether the
    # generator wrote `SUM(x) * 0.1` or `SUM( x )*0.1`. Dropping all whitespace makes
    # the containment check robust to formatting, the dominant cause of false fails.
    return re.sub(r"\s+", "", sql_fragment).lower()


def _expr_in_sql(expr: str, sql: str, sql_norm: str, model: SemanticModel | None = None) -> bool:
    """Is the definition's expression applied in the SQL? Compared as canonical
    trees (``sql_canon.carries``): qualifiers, quoting, identifier case,
    parentheses, the order of ANDed conditions and COUNT(*) against a count of a
    declared key column are form, not meaning. Compared as text, the compiled
    derived metric (every sibling wrapped in parentheses) read as departing from
    the definition it implements, and ``name = 'x'`` read as applied inside
    ``team_name = 'x'``. Only when a side does not parse is the text all there is.
    *model* supplies the dialect and the declared keys."""
    carried = sql_canon.carries(sql, expr, model)
    if carried is not None:
        return carried
    return _norm(expr) in sql_norm


def _touches(column: str, sql: str) -> bool:
    """Does the SQL read or project this column? Word-boundary on the raw text,
    because the SQL a departure produces is often the half that does NOT parse
    cleanly, and a governance signal must not fall silent on a parse error."""
    return bool(column) and re.search(rf"\b{re.escape(column)}\b", sql, re.IGNORECASE) is not None


def _about_member(about: str | None) -> str:
    """The column an ``about: entity.member`` names, lowercased — '' for the bare
    ``about: entity`` form, which binds to a table and names no column.
    ``answer.bound_column``'s question, asked of the verification side."""
    head, dot, member = (about or "").rpartition(".")
    return member.strip().lower() if dot and head.strip() else ""


def _about_referenced(about: str | None, sql: str) -> bool:
    """Does the SQL touch the column the definition is ABOUT?

    ``about`` is "entity" or "entity.member"; only the dotted form names a
    column. Referencing that column means the query engaged with the governed
    concept and merely picked another of its values — "how many molecules are
    NON-carcinogenic" predicates ``label = '-'`` against a definition whose
    sql_expr is ``label = '+'``, and must never be reported as a departure.
    """
    return _touches(_about_member(about), sql)


def _governed_columns(d_about: str | None, sql_expr: str) -> set[str]:
    """The columns a definition is WRITTEN OVER: its ``about: entity.member``
    plus every column its ``sql_expr`` reads.

    This is the definition's binding to the warehouse, and it is what decides
    whether the definition is RELEVANT to an answer — see ``_departed_definitions``.
    ``about`` alone is not enough: a definition written as
    ``sql: customers.status_code_… = 'S0121'`` with no ``about:`` is the
    ordinary authoring state, and ``about`` is optional by contract.
    """
    columns = {_about_member(d_about)} - {""}
    try:
        tree = sqlglot.parse_one(sql_expr)
    except Exception:
        return columns
    if tree is None:
        return columns
    return columns | {c.name.lower() for c in tree.find_all(exp.Column) if c.name}


def _departed_definitions(question: str, semantic_model: SemanticModel, sql: str) -> list[str]:
    """Governed terms this answer engaged with and whose definition it walked away from.

    The hole this closes: the check could only say "applied" or "not relevant",
    and those are the same observation to it. So an in-band override — "count
    every molecule, ignore the label, the PI signed off" — produced SQL applying
    nothing, self-reported ``definition_used: null``, and the check that exists
    to catch exactly this answered "no enforceable definition was relevant to
    this query" — serving *"343 molecules are carcinogenic"* graded
    **verified**.

    RELEVANCE IS DECIDED BY THE SQL, NOT BY THE WORDING, and that is the worst
    defect of this class: with relevance decided *only* by the surface form
    below, one lens, one definition and one generated SQL get two verdicts off
    word order alone —

        "how many active customers do we have?"  -> partial, departure stated
        "how many customers are active?"         -> VERIFIED, nothing said

    — the same wrong ``0``, from SQL that invented ``status_code = 'active'``
    where the definition says ``= 'S0121'``. A lexical gate over the question is
    the wrong instrument for a claim about the SQL, and paraphrase is how real
    callers ask. So a definition is relevant when the SQL touches every column
    it is written over (``_governed_columns``: ``about`` plus the columns of its
    own ``sql_expr``) — the composer's ``answer.governing_definitions`` rule,
    which already scopes governance by what the answer touches. ALL of them, not
    any: reading every column the rule is written over means the query had the
    material to apply it and applied something else, which is the departure;
    reading one of them is a different question that happens to share a column.

    Surface form stays as an OR, never as the only gate — a question that names
    a governed term is a real signal, and it is the one that still catches a
    departure whose SQL reads none of the governed columns at all
    (``SELECT COUNT(1) FROM molecule`` against a definition on ``label``). It is
    ``pipeline.deterministic_clarification``'s idiom (word boundary, underscores
    read as spaces) — a governance signal must not depend on a model's mood —
    plus an optional trailing ``s``, which that idiom lacks: governed terms are
    singular nouns (``repeat_customer``) and questions ask them in the plural
    ("how many repeat customers"), so without it the check misses the ordinary
    phrasing.

    The bias is filter_guard's, for the same reason: UNDER-detection is
    acceptable, a falsely flagged correct answer is not. So relevance is never
    departure on its own — the expression must ALSO be absent AND the column the
    definition is ``about`` go untouched. That last clause is what keeps the SQL
    gate from flagging the correctly-engaged case: "how many molecules are
    NON-carcinogenic" reads ``label``, so the new gate finds it relevant, and
    ``_about_referenced`` then exempts it for predicating the governed column.
    Ambiguous terms are left to the clarification path that already refuses to
    guess them.

    Known over-detection, inherited not introduced: an ``about``-less definition
    whose expression the model wrote to a different literal form (``* 0.1`` for
    ``* 0.10``) reads as a departure. That is ``_expr_in_sql``'s existing
    tolerance, and the claimed-definition branch above has always failed the same
    way; the cost is one conservative grade, never a refused answer. Authoring an
    ``about`` removes it.

    Known over-detection, WIDENED here and stated plainly because it is the price
    of the fix: on an ``about``-less definition the SQL gate cannot tell "picked
    another value of the governed column" from "invented a rule over it". They
    are the same SQL. ``repeat_customer`` = ``number_of_orders > 1`` with no
    ``about`` will now flag on "customers with more than 5 orders", because that
    query reads the governed column and applies a different threshold. The note
    it earns is true — the answer is not governed by the lens's rule — and the
    grade is ``partial``, never a refusal. The escape hatch is the same one and
    it is one line: author ``about: customers.number_of_orders`` and
    ``_about_referenced`` exempts it, exactly as it does for the
    "non-carcinogenic" case above. Under-detection stays the bias wherever the
    two can be told apart; here they cannot, and a silently-wrong ``verified``
    is the worse of the two errors.
    """
    q = question.lower().replace("_", " ")
    sql_norm = _norm(sql)
    departed: list[str] = []
    for d in semantic_model.definitions:
        expr = (d.sql_expr or "").strip()
        if not expr or d.status == "ambiguous":
            continue
        term = d.term.replace("_", " ").lower().strip()
        named = bool(term) and re.search(rf"\b{re.escape(term)}s?\b", q) is not None
        columns = _governed_columns(d.about, expr)
        touched = bool(columns) and all(_touches(c, sql) for c in columns)
        if not (named or touched):
            continue
        if _expr_in_sql(expr, sql, sql_norm, semantic_model) or _about_referenced(d.about, sql):
            continue
        departed.append(d.term)
    return departed


def _definition_applied(
    question: str,
    semantic_model: SemanticModel,
    definition_used: str | None,
    sql: str,
    certification: str = "none",
    compiled_metrics: Sequence[str] = (),
) -> VerificationCheck:
    """A definition is *applied* only when its sql_expr is in the executed SQL —
    'a definition exists' is exactly the signal the binary badge over-trusted.

    A COMPILED serve passes on the metric it was compiled from. The sole-metric
    door and typed serving hand the compiler a governed metric name and the SQL
    is the model's own expression, no generator in between — "what is the MRR
    today" served `mrr_eur` by construction and still graded `partial` for "no
    semantic check confirmed the answer", because this check only knew how to
    find a DEFINITION's sql_expr in generated SQL. The names came from the
    model: that is the confirmation. A departure from an enforceable definition
    the SQL engages still fails first.

    A certified serve abstains instead of failing. This check asks whether
    GENERATION honoured a definition, and a certified answer is not generated:
    its SQL was written and approved by a person, and serving it runs no model
    at all. "fail" would assert a departure that cannot have happened, and the
    grade already carves certified out for exactly this reason (see
    ``_with_grade``: only numeric_grounding, not_truncated, freshness and
    date_coverage demote it). Abstaining keeps the two layers saying the same thing, and keeps the
    honest reading — the definition was not checked here, which is not the same
    as checked and clean."""
    if certification == "certified":
        return VerificationCheck(
            name="definition_applied",
            status="skip",
            reason="certified: the SQL is human-approved, not generated from the definition",
        )
    if compiled_metrics:
        departed = _departed_definitions(question, semantic_model, sql)
        if not departed:
            named = ", ".join(f"'{m}'" for m in compiled_metrics)
            return VerificationCheck(
                name="definition_applied",
                status="pass",
                reason=f"compiled from governed metric {named}: the SQL is the model's own",
            )
    enforceable = {d.term: d.sql_expr for d in semantic_model.definitions if d.sql_expr}
    if not enforceable:
        return VerificationCheck(
            name="definition_applied",
            status="skip",
            reason="the lens has no enforceable (sql_expr) definitions",
        )
    sql_n = _norm(sql)
    if definition_used and definition_used in enforceable:
        expr = enforceable[definition_used]
        if expr is not None and _expr_in_sql(expr, sql, sql_n, semantic_model):
            return VerificationCheck(name="definition_applied", status="pass")
        return VerificationCheck(
            name="definition_applied",
            status="fail",
            reason=(
                f"this lens defines '{definition_used}' in SQL, and the answer was "
                "computed without that definition"
            ),
        )
    # Departure is checked BEFORE "some definition was applied", and the order is
    # load-bearing: a lens with two enforceable definitions, one applied and one
    # departed from, short-circuits to `pass` on the applied one and throws the
    # departure away — even though `_departed_definitions` already named it:
    # `region = 'EU'` applied, `status_code` invented, graded `verified`.
    # Reversing the order costs nothing, because a definition whose
    # expression IS in the SQL can never be in `departed` (`_expr_in_sql` exempts
    # it there too). Only reachable often now that relevance is the SQL's: with a
    # question-text gate the masking branch was rarely the binding one.
    departed = _departed_definitions(question, semantic_model, sql)
    if not departed and any(
        expr is not None and _expr_in_sql(expr, sql, sql_n, semantic_model)
        for expr in enforceable.values()
    ):
        return VerificationCheck(name="definition_applied", status="pass")
    if departed:
        terms = ", ".join(f"'{t}'" for t in departed)
        return VerificationCheck(
            name="definition_applied",
            status="fail",
            reason=(
                # "the question asks about" was the old wording and it is now a
                # lie half the time: relevance is the SQL's, so a caller whose
                # question never said the word would be sent hunting their own
                # phrasing for a term that came from the columns.
                f"the answer engages {terms}, which this lens defines in SQL, "
                "and was computed without that definition"
            ),
        )
    return VerificationCheck(
        name="definition_applied",
        status="skip",
        reason="no enforceable definition was relevant to this query",
    )


def _intent_alignment(question: str, sql: str, dialect: str) -> VerificationCheck:
    """Best-effort agg-vs-ask comparison (deterministic only where parseable); fails
    open to skip — never falsely 'verified' (the spec's wrong-but-valid case)."""
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return VerificationCheck(
            name="intent_alignment", status="skip", reason="SQL not parseable for intent"
        )
    if parsed is None:
        return VerificationCheck(
            name="intent_alignment", status="skip", reason="SQL not parseable for intent"
        )
    counts = list(parsed.find_all(exp.Count))
    wants_distinct = bool(_DISTINCT_ASK.search(question))
    asks_count = bool(_COUNT_ASK.search(question))
    if counts:
        has_distinct = any(isinstance(c.this, exp.Distinct) for c in counts)
        if wants_distinct and not has_distinct:
            return VerificationCheck(
                name="intent_alignment",
                status="fail",
                reason="the question asks for distinct/unique values but the SQL counts "
                "all rows (COUNT without DISTINCT)",
            )
        if wants_distinct or asks_count:
            return VerificationCheck(name="intent_alignment", status="pass")
    # Own the limitation: this check's vocabulary is count/
    # distinct asks ONLY. The old reason — "intent not determinable from the
    # question" — blamed a fully explicit question for the check's own
    # narrowness, which misread as the system having tried and failed.
    return VerificationCheck(
        name="intent_alignment",
        status="skip",
        reason="outside this check's vocabulary (it verifies count/distinct asks only) "
        "— not a claim about the question's clarity",
    )


def _lens_today(timezone: str | None) -> date:
    try:
        return (datetime.now(ZoneInfo(timezone)) if timezone else datetime.now(UTC)).date()
    except Exception:
        return datetime.now(UTC).date()


def _snapshot_pin_only(sql: str, dialect: str | None, time_fields: set[str]) -> bool:
    """True when every predicate on a time field is an equality pin to a
    MAX(time_field) subquery — the latest-snapshot idiom."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(tree, exp.Expression):
        return False
    lowered = {f.lower() for f in time_fields if f}
    pins = 0
    for eq in tree.find_all(exp.EQ):
        col = eq.this
        if isinstance(col, exp.Column) and col.name.lower() in lowered:
            sub = eq.expression
            if isinstance(sub, exp.Subquery | exp.Paren) and any(
                isinstance(n, exp.Max) for n in sub.find_all(exp.Max)
            ):
                pins += 1
            else:
                return False  # a real comparison on the time field — not pin-only
    return pins > 0


def _level_columns(entity: Entity) -> dict[str, str]:
    """Aggregated column -> snapshot time field, for every level metric of
    *entity* that aggregates one plain column (``entity.col`` or ``col``)."""
    out: dict[str, str] = {}
    for m in entity.metrics:
        if m.additivity != "level" or not m.expr:
            continue
        time_field = m.agg_time_field or entity.default_time_field
        if not time_field or not re.fullmatch(r"[\w.]+", m.expr):
            continue
        out[m.expr.rpartition(".")[2].lower()] = time_field.lower()
    return out


def _aggregates_only_levels(sql: str, model: SemanticModel) -> bool:
    """True when every aggregate the statement projects (the pin's own
    ``MAX(time_field)`` subselect aside) is over a declared level metric's
    column — the shape the latest snapshot legitimately answers."""
    levels = {col for e in model.entities for col in _level_columns(e)}
    if not levels:
        return False
    try:
        tree = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(tree, exp.Expression):
        return False
    seen = False
    for agg in tree.find_all(exp.AggFunc):
        if agg.find_ancestor(exp.Predicate) is not None:
            continue  # the snapshot pin's scalar subselect
        cols = {c.name.lower() for c in agg.find_all(exp.Column)}
        if not cols or not cols <= levels:
            return False
        seen = True
    return seen


_BUCKET_UNITS = frozenset({"week", "month", "quarter", "year"})


def _bucket_units(tree: exp.Expression, time_field: str) -> set[str]:
    """The calendar units the statement buckets *time_field* into —
    DATE_TRUNC/TIMESTAMP_TRUNC or EXTRACT over it, week or wider."""
    units: set[str] = set()
    for node in tree.find_all(exp.DateTrunc, exp.TimestampTrunc, exp.Extract):
        if isinstance(node, exp.Extract):
            unit, operand = node.name, node.expression
        else:
            unit_node = node.args.get("unit")
            unit, operand = (unit_node.name if unit_node is not None else ""), node.this
        if operand is None:
            continue
        over = {c.name.lower() for c in operand.find_all(exp.Column)}
        if isinstance(operand, exp.Column):
            over = {operand.name.lower()}
        if time_field in over and unit.lower() in _BUCKET_UNITS:
            units.add(unit.lower())
    return units


def _latest_per_bucket(tree: exp.Expression, time_field: str) -> bool:
    """Does the statement pick the latest snapshot per bucket — a ``MAX(time_field)``
    subselect (the compiler's form), or a window ordered by the time field
    (the ROW_NUMBER / QUALIFY idiom)?"""
    for mx in tree.find_all(exp.Max):
        if any(c.name.lower() == time_field for c in mx.find_all(exp.Column)):
            return True
    for win in tree.find_all(exp.Window):
        order = win.args.get("order")
        if order is not None and any(
            c.name.lower() == time_field for c in order.find_all(exp.Column)
        ):
            return True
    return False


def _global_pin(tree: exp.Expression, time_field: str) -> bool:
    """Does the statement pin the snapshot to ONE latest day — a ``MAX(time_field)``
    subselect that groups by nothing and refers to no other table alias (so
    nothing correlates it to a bucket of the outer query)?"""
    for sel in tree.find_all(exp.Select):
        maxes = [
            mx
            for proj in sel.expressions
            for mx in proj.find_all(exp.Max)
            if any(c.name.lower() == time_field for c in mx.find_all(exp.Column))
        ]
        if not maxes or sel.args.get("group") is not None:
            continue
        aliases = {c.table for c in sel.find_all(exp.Column) if c.table}
        if len(aliases) <= 1:
            return True
    return False


def _level_grain(
    sql: str, model: SemanticModel, certification: str = "none", question: str = ""
) -> VerificationCheck:
    """Is a level metric read as the latest snapshot per time bucket?

    A stock (ARR, headcount, a balance) is declared ``additivity: level``: a
    month's figure is the month's LAST snapshot. SUM over its column grouped
    by a month/quarter/year bucket of the snapshot field adds every snapshot
    in the bucket — thirty daily balances served as one monthly balance, a
    well-formed query over well-formed rows that no other check can see.
    Deterministic and model-declared: fires only on SUM over a declared level
    column, a bucket wider than a day over its time field, and no
    latest-per-bucket selection anywhere in the statement. The mirror shape
    fires on the question's breakdown phrase: "per month" over a level column
    whose snapshot is pinned to ONE latest day (``MAX(time_field)`` correlated
    to nothing) is the series collapsed to its last day, one row served as a
    year. Certified SQL is approved as meaning what it says and is not re-read
    here."""
    if certification == "certified":
        return VerificationCheck(
            name="level_grain", status="skip", reason="certified SQL: approval binds the reading"
        )
    levels = {col: (e, tf) for e in model.entities for col, tf in _level_columns(e).items()}
    if not levels:
        return VerificationCheck(
            name="level_grain", status="skip", reason="no level metric in the model"
        )
    try:
        tree = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001
        return VerificationCheck(name="level_grain", status="skip", reason="SQL did not parse")
    if not isinstance(tree, exp.Expression):
        return VerificationCheck(name="level_grain", status="skip", reason="SQL did not parse")
    unit = breakdown_unit(question)
    if unit is not None:
        present = {c.name.lower() for c in tree.find_all(exp.Column)}
        for col, (_entity, time_field) in levels.items():
            if col in present and _global_pin(tree, time_field):
                return VerificationCheck(
                    name="level_grain",
                    status="fail",
                    reason=(
                        f"the question asks for '{col}' per {unit}, but the SQL pins "
                        f"'{time_field}' to one latest snapshot for the whole series "
                        f"(MAX({time_field}) correlated to no {unit}) — that collapses "
                        f"every {unit} to the last day; read the latest snapshot "
                        f"inside each {unit}"
                    ),
                )
    if tree.find(exp.Group) is None:
        return VerificationCheck(
            name="level_grain", status="skip", reason="no grouped aggregate in this query"
        )
    summed = [
        (col, e, tf)
        for col, (e, tf) in levels.items()
        if any(
            any(c.name.lower() == col for c in sm.find_all(exp.Column))
            for sm in tree.find_all(exp.Sum)
        )
    ]
    if not summed:
        return VerificationCheck(
            name="level_grain", status="skip", reason="no level metric is summed here"
        )
    for col, entity, time_field in summed:
        units = _bucket_units(tree, time_field)
        # A table whose declared grain IS the bucket ("one row per customer per
        # month") holds one snapshot per bucket: nothing to collapse.
        units -= {u for u in units if u in (entity.grain or "").lower()}
        if not units or _latest_per_bucket(tree, time_field):
            continue
        unit = sorted(units)[0]
        return VerificationCheck(
            name="level_grain",
            status="fail",
            reason=(
                f"'{col}' is a level metric (a snapshot) but the SQL SUMs it per "
                f"{unit} over '{time_field}' — that adds every snapshot in the "
                f"{unit}; read the latest snapshot inside each {unit} instead "
                f"({time_field} = (SELECT MAX({time_field}) … in the same {unit}))"
            ),
        )
    return VerificationCheck(name="level_grain", status="pass")


_LITERAL_DAY = re.compile(
    r"((?:19|20)\d{2})-(0[1-9]|1[0-2])"
    r"(?:-(0[1-9]|[12]\d|3[01])(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?)?"
)
_TRUNC_UNITS = ("day", "week", "month", "quarter", "year")


def _unit_end(day: date, unit: str) -> date:
    if unit == "day":
        return day
    if unit == "week":
        return day + timedelta(days=6)
    months = {"month": 1, "quarter": 3, "year": 12}[unit]
    total = day.year * 12 + day.month - 1 + months
    return date(total // 12, total % 12 + 1, 1) - timedelta(days=1)


def _literal_span(node: exp.Expression) -> tuple[date, date, bool] | None:
    """``(first day, last day, at midnight)`` a date literal names, through
    any cast: '2026-08-28' is one day, '2026-08' (a month-form period) the
    whole month; ``at midnight`` is False for a literal with a later time."""
    while isinstance(node, exp.Cast | exp.TryCast | exp.Date | exp.TsOrDsToDate):
        node = node.this
    if not (isinstance(node, exp.Literal) and node.is_string):
        return None
    m = _LITERAL_DAY.fullmatch(node.this.strip())
    if m is None:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        first = date(y, mo, int(d) if d else 1)
    except ValueError:
        return None
    if not d:
        return first, _unit_end(first, "month"), True
    midnight = not any(int(g or 0) for g in m.groups()[3:])
    return first, first, midnight


def _column_unit(node: exp.Expression) -> tuple[str, str] | None:
    """``(column, unit)`` for a column, a cast of one, or a calendar
    truncation of one (``DATE_TRUNC('month', day)`` → ``('day', 'month')``)."""
    unit = "day"
    while not isinstance(node, exp.Column):
        if isinstance(node, exp.Cast | exp.TryCast | exp.Date | exp.TsOrDsToDate):
            node = node.this
        elif isinstance(node, exp.DateTrunc | exp.TimestampTrunc | exp.DatetimeTrunc):
            raw = node.args.get("unit")
            unit = (raw.name if raw is not None else "").lower()
            if unit not in _TRUNC_UNITS:
                return None
            node = node.this
        else:
            return None
    return node.name.lower(), unit


def _date_bounds(
    sql: str, dialect: str | None, dates: frozenset[str] = frozenset()
) -> dict[str, tuple[date, date]]:
    """The days each column's WHERE predicates admit, read off its date
    literals — only columns bounded on both sides. ``dates`` names the
    columns declared DATE, where ``<= '2026-10-12'`` holds the 12th; on any
    other column the reading is the narrowest the SQL allows (``<=`` a
    midnight ends the day before). Predicates under an OR are not read, and
    an unparseable query bounds nothing: a caller failing on a WIDER window
    then never fails on a guess."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's problem
        return {}
    if not isinstance(tree, exp.Expression):
        return {}
    lows: dict[str, date] = {}
    highs: dict[str, date] = {}

    def bound(col: str, lo: date | None, hi: date | None) -> None:
        if lo is not None:
            lows[col] = max(lo, lows.get(col, lo))
        if hi is not None:
            highs[col] = min(hi, highs.get(col, hi))

    one = timedelta(days=1)
    for where in tree.find_all(exp.Where):
        for node in where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
            ancestor = node.parent
            ored = False
            while ancestor is not None and ancestor is not where:
                if isinstance(ancestor, exp.Or | exp.Not | exp.Case | exp.If):
                    ored = True
                    break
                ancestor = ancestor.parent
            if ored:
                continue
            side = _column_unit(node.this)
            if side is None:
                continue
            col, unit = side
            whole_day = col in dates and unit == "day"
            if isinstance(node, exp.Between):
                lo_lit = _literal_span(node.args["low"])
                hi_lit = _literal_span(node.args["high"])
                if lo_lit and hi_lit:
                    early = hi_lit[2] and hi_lit[0] == hi_lit[1] and not whole_day
                    bound(col, lo_lit[0], hi_lit[1] - one if early else hi_lit[1])
                continue
            lit = _literal_span(node.expression)
            if lit is None:
                continue
            first, last, midnight = lit
            if isinstance(node, exp.EQ):
                bound(col, first, _unit_end(last, unit))
            elif isinstance(node, exp.GTE):
                bound(col, first, None)
            elif isinstance(node, exp.GT):
                bound(col, last + one, None)
            elif isinstance(node, exp.LT):
                bound(col, None, first - one if midnight else first)
            else:  # LTE
                early = midnight and first == last and not whole_day
                bound(col, None, last - one if early else last)
    return {c: (lows[c], highs[c]) for c in lows.keys() & highs.keys()}


def _window_applied(
    question: str,
    sql: str,
    model: SemanticModel,
    certification: str = "none",
    certified_question: str | None = None,
) -> VerificationCheck:
    """Did the question's time window reach the SQL at all?

    The highest-severity wrong-answer shape observed: "last month" generated
    SQL with NO date predicate, and an all-history 8,740 served as a monthly
    figure at `verified` — numeric_grounding and intent_alignment both
    correctly pass (the number IS in the rows; the question IS about churn),
    because neither looks at the period. This check is deliberately COARSE:
    it fails only when the question states a window (from a closed
    vocabulary) and the SQL shows no date handling anywhere — no date
    function, no date/year literal, no declared time field. A fail caps at
    `partial` (never unverified): a legitimately pre-windowed source view is
    the false-positive to stay safe against, and the reason says so."""
    terms = temporal_terms(question)
    dates = frozenset(f.name.lower() for e in model.entities for f in e.fields if f.type == "date")
    # Part of a month the window parser could not turn into days ("early
    # August", "the first few days of August") is still a day-level ask: SQL
    # whose every date-bounded column covers that whole month answers wider.
    partial = unplaced_days(question, _lens_today(model.timezone))
    if partial and not (
        certification == "certified"
        and certified_question is not None
        and unplaced_days(certified_question, _lens_today(model.timezone)) == partial
    ):
        bounds = _date_bounds(sql, model.dialect, dates)
        if bounds and all(
            any(lo <= first and hi >= last for _p, first, last in partial)
            for lo, hi in bounds.values()
        ):
            return VerificationCheck(
                name="window_applied",
                status="fail",
                reason=(
                    "the question asks about "
                    + ", ".join(f"'{phrase}'" for phrase, _a, _b in partial)
                    + " but the SQL's date filters cover the whole month — a wider window "
                    "than the days asked"
                ),
            )
    if not terms:
        return VerificationCheck(
            name="window_applied", status="skip", reason="the question states no time window"
        )
    if certification == "certified" and certified_question is not None:
        # Approval vouches for the SQL's meaning, the window included — when
        # the approved question states the SAME window as the ask (a snapshot
        # table's approved SQL legitimately carries no date handling at all).
        # A fuzzy match onto another window keeps the deterministic read below.
        if temporal_terms(certified_question) == terms:
            return VerificationCheck(
                name="window_applied",
                status="pass",
                reason="certified SQL: the approved question states the asked window",
            )
    parts = year_parts(terms)
    if parts is not None and not {1, 2, 3, 4} <= parts:
        # A quarter or a half served as its whole year: "the third quarter of
        # 2026" answered by `>= '2026-01-01' AND <= '2026-12-31'` is the
        # year's figure. Fails only when every column the SQL bounds by date
        # literals on both sides admits a whole calendar year.
        bounds = _date_bounds(sql, model.dialect, dates)
        if bounds and all(
            any(lo <= date(y, 1, 1) and hi >= date(y, 12, 31) for y in range(lo.year, hi.year + 1))
            for lo, hi in bounds.values()
        ):
            served = "; ".join(
                f"{col} {lo.isoformat()} to {hi.isoformat()}"
                for col, (lo, hi) in sorted(bounds.items())
            )
            return VerificationCheck(
                name="window_applied",
                status="fail",
                reason=(
                    f"the question asks about part of a year ({', '.join(sorted(terms))}) but "
                    f"the SQL's date filters cover {served} — a whole year, a wider window "
                    "than asked"
                ),
            )
    time_fields = {e.default_time_field for e in model.entities if e.default_time_field}
    time_fields |= {
        f.name
        for e in model.entities
        for f in e.fields
        if "date" in (f.type or "").lower() or "time" in (f.type or "").lower()
    }
    if sql_engages_time(sql, time_fields) and is_day_window(terms, relative=False):
        # A day or a span of days must not be served as the month around it:
        # "on 28 August 2026" answered by `>= '2026-08-01' AND <= '2026-08-31'`
        # is August's figure. Only a day the question writes out: "today"
        # served as the latest loaded day is a snapshot reading, not a
        # widening. Fails only when every column the SQL bounds on
        # both sides by date literals admits days outside the asked ones —
        # relative SQL (CURRENT_DATE arithmetic) is not read, never guessed.
        if _aggregates_only_levels(sql, model):
            # A level metric at a month's end reads the month's last
            # snapshot: the month's bounds are the reading, not a widening.
            terms = month_end_as_month(terms)
        ranges = window_ranges(terms, _lens_today(model.timezone))
        bounds = _date_bounds(sql, model.dialect, dates)
        if len(ranges) == 1 and bounds and is_day_window(terms, relative=False):
            start, end = ranges[0]
            if all(lo < start or hi > end for lo, hi in bounds.values()):
                served = "; ".join(
                    f"{col} {lo.isoformat()} to {hi.isoformat()}"
                    for col, (lo, hi) in sorted(bounds.items())
                )
                return VerificationCheck(
                    name="window_applied",
                    status="fail",
                    reason=(
                        f"the question asks about {start.isoformat()} to {end.isoformat()} "
                        f"but the SQL's date filters cover {served} — a wider window than "
                        "the days asked"
                    ),
                )
    if sql_engages_time(sql, time_fields):
        # The latest-snapshot-pin refinement: a question about a
        # CALENDAR PERIOD ("last month") whose SQL's only date handling is
        # `time_field = (SELECT MAX(time_field) …)` pins one snapshot day, not
        # a period — "how much churned last month" answered from the latest
        # daily row PASSED here, 40% wrong. Fires only when the ask is a
        # period grain (never day-level) AND no real date arithmetic or
        # literal exists anywhere — a genuinely windowed query keeps passing.
        period_ask = any(
            g in t
            for t in terms
            for g in ("week", "month", "quarter", "year", "q1", "q2", "q3", "q4", "h1", "h2")
        )
        from services.runtime.timewindow import _SQL_DATE_EVIDENCE

        if (
            period_ask
            and not _SQL_DATE_EVIDENCE.search(sql)
            and _snapshot_pin_only(sql, model.dialect, time_fields)
        ):
            # The one pin that IS the period's figure: a level metric (a
            # stock, read as its latest snapshot) asked for the CURRENT
            # period — the latest snapshot lies inside the stated window, and
            # freshness_check owns how old it is. A flow (churn, bookings)
            # summed from one day still fails, as does any past period.
            today = _lens_today(model.timezone)
            ranges = window_ranges(terms, today)
            current = bool(ranges) and all(lo <= today <= hi for lo, hi in ranges)
            if current and _aggregates_only_levels(sql, model):
                return VerificationCheck(
                    name="window_applied",
                    status="pass",
                    reason=(
                        "the SQL pins the latest snapshot of a level metric, which is "
                        "the stated current period's figure"
                    ),
                )
            stated = ", ".join(sorted(terms))
            return VerificationCheck(
                name="window_applied",
                status="fail",
                reason=(
                    f"the question asks about '{stated}' but the SQL only pins the "
                    "latest snapshot date (time_field = MAX(time_field)) — one day's "
                    "row, not the asked period"
                ),
            )
        return VerificationCheck(name="window_applied", status="pass")
    stated = ", ".join(sorted(terms))
    return VerificationCheck(
        name="window_applied",
        status="fail",
        reason=(
            f"the question asks about '{stated}' but the SQL carries no date handling "
            "at all — the figure may cover all history, not the asked period (if the "
            "source is pre-windowed, declare its time field so this can pass)"
        ),
    )


def ratio_population_mismatch(sql: str, dialect: str | None) -> str | None:
    """A ratio whose DENOMINATOR filters on the very column the NUMERATOR
    aggregates unfiltered mixes populations: SUM(quota) over ALL
    rows divided by COUNT(DISTINCT rep) over rows WHERE quota > 0 serves
    a figure an order of magnitude off — one that is not a valid measure of
    anything, identical across model tiers.

    Deliberately narrow (rule: serve-time guards must not false-positive):
    share-of-total ratios (numerator-subset) pass, and 'total revenue per
    active rep' (denominator filtered on a DIFFERENT column) passes — only
    the shape where the denominator's predicate names the numerator's own
    aggregated column fires. Returns the offending predicate for the repair
    prompt, else None."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's problem
        return None
    if not isinstance(tree, exp.Expression):
        return None

    def _agg_info(side: exp.Expression) -> tuple[set[str], set[str], list[exp.Expression]] | None:
        """(columns aggregated, columns predicated inside the aggregate,
        predicate nodes) — None when the side carries no aggregate."""
        aggs = list(side.find_all(exp.AggFunc))
        if not aggs:
            return None
        agg_cols: set[str] = set()
        pred_cols: set[str] = set()
        preds: list[exp.Expression] = []
        for agg in aggs:
            conditions: list[exp.Expression] = []
            for case_node in agg.find_all(exp.Case):
                for if_node in case_node.args.get("ifs") or []:
                    conditions.append(if_node.this)
            if isinstance(agg.parent, exp.Filter):
                fw = agg.parent.expression
                if isinstance(fw, exp.Where):
                    conditions.append(fw.this)
            for cond in conditions:
                preds.append(cond)
                pred_cols.update(c.name.lower() for c in cond.find_all(exp.Column))
            for col in agg.find_all(exp.Column):
                name = col.name.lower()
                if name not in pred_cols or not conditions:
                    agg_cols.add(name)
        return agg_cols, pred_cols, preds

    for div in tree.find_all(exp.Div):
        num, den = div.this, div.expression
        while isinstance(num, exp.Cast | exp.Paren):
            num = num.this
        while isinstance(den, exp.Cast | exp.Paren):
            den = den.this
        if isinstance(den, exp.Func) and den.sql_name().upper() == "NULLIF":
            den = den.this
        num_info, den_info = _agg_info(num), _agg_info(den)
        if num_info is None or den_info is None:
            continue
        num_agg_cols, num_pred_cols, _ = num_info
        _den_agg, _den_pred, den_preds = den_info
        for pred in den_preds:
            pred_cols = {c.name.lower() for c in pred.find_all(exp.Column)}
            hit = pred_cols & num_agg_cols
            if hit and not num_pred_cols:
                return (
                    f"the ratio's denominator filters on {pred.sql(dialect=dialect)} while the "
                    f"numerator aggregates {', '.join(sorted(hit))} over ALL rows — the two "
                    "sides measure different populations; apply the same predicate to both, "
                    "or say which population is intended"
                )
    return None


def _ratio_population(sql: str, dialect: str | None) -> VerificationCheck:
    mismatch = ratio_population_mismatch(sql, dialect)
    if mismatch is None:
        return VerificationCheck(name="ratio_population", status="pass")
    return VerificationCheck(name="ratio_population", status="fail", reason=mismatch)


def _zero_ratio_plausibility(sql: str, result: QueryResult) -> VerificationCheck:
    """An exact 0 from dividing SQL is almost always an empty numerator window,
    not a finding.

    The observed shape: a CURRENT_DATE predicate on a snapshot whose newest
    partition is 1–2 days old matches nothing; SAFE_DIVIDE composes a
    confident 0.0; grounding passes because 0.0 IS in the result — a
    plausibility gap, not a faithfulness gap, so it gets its own check. Fails
    (→ partial, never unverified) only on the narrow shape: a single-row
    result whose value is exactly 0 from SQL that divides. A genuine zero
    ratio keeps its answer and loses only the top badge, with the reason
    naming what to verify."""
    divides = bool(re.search(r"SAFE_DIVIDE|[\w)\s]/\s*[\w(]", sql, re.IGNORECASE))
    if not divides or len(result.rows) != 1:
        return VerificationCheck(
            name="zero_ratio", status="skip", reason="not a single-row ratio result"
        )
    # decimal.Decimal counts as numeric (NUMERIC money columns arrive as Decimal).
    numeric = [
        c
        for c in result.rows[0]
        if isinstance(c, int | float | decimal.Decimal) and not isinstance(c, bool)
    ]
    if not numeric or any(float(c) != 0.0 for c in numeric):
        return VerificationCheck(name="zero_ratio", status="pass")
    return VerificationCheck(
        name="zero_ratio",
        status="fail",
        reason=(
            "a ratio computed exactly 0 — with a non-empty denominator this is "
            "almost always a numerator window that matched no rows (e.g. "
            "CURRENT_DATE against a lagging snapshot), not a real zero; verify "
            "the numerator's date filter against the data's latest partition"
        ),
    )


_PCT_COLUMN = re.compile(r"(^|_)(rate|pct|percent|percentage|ratio|share)(_|$)", re.IGNORECASE)


def _all_ones_ratio(sql: str, result: QueryResult) -> VerificationCheck:
    """A ratio that is exactly 1.0 (or 100.0) on EVERY row is a predicate
    pinning both sides of the division to the same population, not a finding.

    The observed shape: a definition's predicate ("on time" = delivered on or
    before due) ANDed into the WHERE of an on-time RATE — the denominator is
    then only the on-time rows too, and the rate reads 100% for every carrier,
    served verified. Plan rejects the authoring shape; this is the serve-time
    tripwire for whatever reaches the warehouse anyway. Candidates are the
    rate/pct-named columns; with none named and SQL that divides, every
    numeric column. Fails only when every candidate value across more than
    one row is exactly 1 or 100 — a single row cannot show the pattern and is
    a skip, never a vacuous pass. A genuine all-perfect result keeps its
    answer and loses only the top badge, with the reason naming what to check."""
    divides = bool(re.search(r"SAFE_DIVIDE|[\w)\s]/\s*[\w(]", sql, re.IGNORECASE))
    named = [i for i, c in enumerate(result.columns) if _PCT_COLUMN.search(c)]
    if not named and not divides:
        return VerificationCheck(name="all_ones_ratio", status="skip", reason="not a ratio result")
    if len(result.rows) < 2:
        # One row cannot show the per-row pattern; the SQL can. A ratio of
        # exactly 1 whose numerator and denominator are filtered by the same
        # predicate is the same pin, read statically — deterministic, so it
        # fails; anything less certain stays a skip.
        cells = [
            v
            for v in (result.rows[0] if result.rows else [])
            if isinstance(v, int | float | decimal.Decimal) and not isinstance(v, bool)
        ]
        shared = _shared_ratio_filter(sql) if any(float(v) in (1.0, 100.0) for v in cells) else None
        if shared is not None:
            return VerificationCheck(
                name="all_ones_ratio",
                status="fail",
                reason=f"numerator and denominator share the filter ({shared}) — the "
                "ratio is pinned to 1 by construction, not measured",
            )
        return VerificationCheck(
            name="all_ones_ratio",
            status="skip",
            reason="a single-row result cannot show a ratio pinned to 1 on every row",
        )
    candidates = named or [
        i
        for i in range(len(result.columns))
        if any(
            isinstance(row[i], int | float | decimal.Decimal) and not isinstance(row[i], bool)
            for row in result.rows
            if i < len(row)
        )
    ]
    values = [
        row[i] for row in result.rows for i in candidates if i < len(row) and row[i] is not None
    ]
    numeric = [
        float(v)
        for v in values
        if isinstance(v, int | float | decimal.Decimal) and not isinstance(v, bool)
    ]
    if not numeric or len(numeric) != len(values) or any(v not in (1.0, 100.0) for v in numeric):
        return VerificationCheck(name="all_ones_ratio", status="pass")
    cols = ", ".join(result.columns[i] for i in candidates)
    return VerificationCheck(
        name="all_ones_ratio",
        status="fail",
        reason=(
            f"{cols} is exactly {'100' if 100.0 in numeric else '1'} on every one of "
            f"{len(result.rows)} rows — a ratio whose numerator and denominator were "
            "filtered to the same population (a definition's predicate ANDed under the "
            "division), not a result; check the WHERE clause against the ratio's "
            "denominator"
        ),
    )


def _aggregate_condition(node: exp.Expression) -> exp.Expression | None:
    """The predicate an aggregate counts under: its FILTER (WHERE …) clause,
    or the single WHEN of a CASE it aggregates (`SUM(CASE WHEN p THEN 1 END)`,
    `COUNT(CASE WHEN p THEN 1 END)`). None for a plain aggregate."""
    while True:
        if isinstance(node, exp.Paren | exp.Cast | exp.Nullif):
            node = node.this
        elif isinstance(node, exp.Mul | exp.Div) and isinstance(node.expression, exp.Literal):
            node = node.this  # `SUM(…) * 1.0`: the scale is not the aggregate
        elif isinstance(node, exp.Mul) and isinstance(node.this, exp.Literal):
            node = node.expression
        else:
            break
    if isinstance(node, exp.Anonymous) and str(node.name).upper() == "COUNTIF":
        first = node.expressions[0] if node.expressions else None
        return first if isinstance(first, exp.Expression) else None
    if isinstance(node, exp.CountIf):
        this = node.this
        return this if isinstance(this, exp.Expression) else None
    if isinstance(node, exp.Filter):
        where = node.expression
        return where.this if isinstance(where, exp.Where) else None
    if isinstance(node, exp.AggFunc):
        inner = node.this
        while isinstance(inner, exp.Paren | exp.Cast):
            inner = inner.this
        if isinstance(inner, exp.Case):
            ifs = inner.args.get("ifs", [])
            if len(ifs) == 1 and isinstance(ifs[0].this, exp.Expression):
                return ifs[0].this
    return None


def _shared_ratio_filter(sql: str) -> str | None:
    """The predicate a ratio's numerator and denominator BOTH apply, as text,
    or None. Shared when the two aggregates count under the same condition,
    or when one side's condition is also a conjunct of the WHERE clause (the
    denominator is then filtered to the numerator's rows). Parse failures and
    SQL with no division read as not shared — this is a tripwire, never a
    guess."""
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 — unparseable SQL is not a shared filter
        return None
    division = tree.find(exp.Div)
    num_den: tuple[exp.Expression, exp.Expression] | None = None
    if division is not None:
        num_den = (division.this, division.expression)
    else:
        safe = next(
            (
                f
                for f in tree.find_all(exp.Anonymous)
                if str(f.name).upper() == "SAFE_DIVIDE" and len(f.expressions) == 2
            ),
            None,
        )
        if safe is not None:
            num_den = (safe.expressions[0], safe.expressions[1])
    if num_den is None:
        return None
    conditions = [_aggregate_condition(side) for side in num_den]
    where = tree.find(exp.Where)
    clause = where.this if where is not None else None
    parts: list[exp.Expression] = []
    if isinstance(clause, exp.And):
        parts = [c for c in clause.flatten() if isinstance(c, exp.Expression)]
    elif isinstance(clause, exp.Expression):
        parts = [clause]
    conjuncts = [_strip_refs(c.copy()).sql() for c in parts]
    texts = [_strip_refs(c.copy()).sql() if c is not None else None for c in conditions]
    if texts[0] is not None and texts[0] == texts[1]:
        return texts[0]
    for t in texts:
        if t is not None and t in conjuncts:
            return t
    return None


def _strip_refs(node: exp.Expression) -> exp.Expression:
    """Qualifiers and identifier quoting off a parsed expression, in place —
    the canonical treatment for predicate comparison."""
    for col in node.find_all(exp.Column):
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    for ident in node.find_all(exp.Identifier):
        ident.set("quoted", False)
    return node


def _predicate_forms(node: exp.Expression) -> set[str]:
    """Canonical spellings of one predicate: sqlglot's canonical SQL (which
    already folds DATE 'x' and CAST('x' AS DATE) together) plus a cast-unwrapped
    variant, so `>= '2025-09-01'` and `>= DATE '2025-09-01'` also meet."""
    while isinstance(node, exp.Paren):  # the compiler paren-wraps each conjunct
        node = node.this
    stripped = _strip_refs(node.copy())
    forms = {_norm(stripped.sql())}
    unwrapped = stripped.copy()
    for cast_node in list(unwrapped.find_all(exp.Cast)):
        cast_node.replace(cast_node.this)
    forms.add(_norm(unwrapped.sql()))
    return forms


def _predicate_in_sql(expr: str, sql: str, dialect: str | None) -> bool:
    """Is the declared predicate applied in the SQL, compared as PARSED
    expressions rather than text?

    A text compare fails the compiler's OWN rendering — the author writes
    ``DATE '2025-09-01'``, the compiler emits ``CAST('2025-09-01' AS DATE)``,
    and declaring population_filter would permanently cap every answer at
    `partial` with a false scope-violation reason: a direct disincentive to
    adopt the mechanism that generalizes best. Both sides parse in the model's
    dialect, qualifiers and casts normalize, and the declared predicate must
    appear among the statement's WHERE/QUALIFY/HAVING conjuncts."""
    try:
        want = _predicate_forms(cast(exp.Expression, sqlglot.parse_one(expr, read=dialect)))
        stmt = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return False
    if stmt is None:
        return False
    have: set[str] = set()
    for home in (exp.Where, exp.Qualify, exp.Having):
        for clause in stmt.find_all(home):
            for conjunct in (
                clause.this.flatten() if isinstance(clause.this, exp.And) else [clause.this]
            ):
                have |= _predicate_forms(cast(exp.Expression, conjunct))
    return bool(want & have)


def period_dates(period: str) -> tuple[date, date] | None:
    """The inclusive date span a definition's ``period`` names: 'YYYY-MM' is
    the month, 'YYYY' the year, 'YYYY-MM-DD/YYYY-MM-DD' both ends as written.
    None for a shape the contract did not admit (never raises on a serve)."""
    text = period.strip()
    try:
        if "/" in text:
            lo, hi = text.split("/", 1)
            return date.fromisoformat(lo), date.fromisoformat(hi)
        if len(text) == 7:
            first = date.fromisoformat(f"{text}-01")
            return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(
                days=1
            )
        if len(text) == 4:
            year = int(text)
            return date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        return None
    return None


_DATE_LITERAL = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_YEAR_LITERAL = re.compile(r"(?<![\d.])((?:19|20)\d{2})(?![\d-])")


def served_window(sql: str) -> tuple[date, date] | None:
    """The span of dates the SQL's own literals bound: the earliest and latest
    date literal it carries; with none, the years it names (Jan 1 to Dec 31);
    with neither, None — an unbounded read, which overlaps every period."""
    found: list[date] = []
    for raw in _DATE_LITERAL.findall(sql):
        try:
            found.append(date.fromisoformat(raw))
        except ValueError:
            continue
    if found:
        return min(found), max(found)
    years = [int(y) for y in _YEAR_LITERAL.findall(sql)]
    if years:
        return date(min(years), 1, 1), date(max(years), 12, 31)
    return None


def _term_named(d: Definition, question: str) -> bool:
    """Does the question name this term, by its identifier or an alias —
    ``_departed_definitions``'s idiom (word boundary, underscores read as
    spaces, an optional plural), the one deterministic surface signal."""
    q = question.lower().replace("_", " ")
    for raw in (d.term, *d.aliases):
        term = raw.replace("_", " ").lower().strip()
        if term and re.search(rf"\b{re.escape(term)}s?\b", q) is not None:
            return True
    return False


def ignored_definitions(
    sql: str, model: SemanticModel, question: str = ""
) -> list[tuple[Definition, str]]:
    """The rules this SQL reads past: every definition carrying a ``sql:``
    predicate, ``about`` an entity the SQL reads, whose predicate the SQL does
    not apply (parsed containment, ``_predicate_in_sql``) — a PERIOD rule
    while the SQL's own window overlaps its period, a rule with no period
    when *question* names the term (a term's predicate governs the questions
    that ask for the term, as on the generated rail; a period's governs every
    read of its dates). Each pair carries the one-line reason. Deterministic:
    no model, and a window the SQL does not state is an overlap, never a
    pass."""
    out: list[tuple[Definition, str]] = []
    window = served_window(sql)
    for d in model.definitions:
        predicate = (d.sql_expr or "").strip()
        head = (d.about or "").partition(".")[0].strip()
        if not predicate or not head or d.status == "ambiguous":
            continue
        if not d.period and not _term_named(d, question):
            continue
        entity = next((e for e in model.entities if e.name.lower() == head.lower()), None)
        if entity is None:
            continue
        if not (_touches(entity.source.table, sql) or _touches(entity.name, sql)):
            continue
        if _predicate_in_sql(predicate, sql, model.dialect):
            continue
        span = period_dates(d.period) if d.period else None
        if span is not None and window is not None and (window[1] < span[0] or window[0] > span[1]):
            continue
        over = (
            f"over {span[0].isoformat()} to {span[1].isoformat()}"
            if span is not None
            else "for a question naming the term"
        )
        out.append(
            (
                d,
                f"reads `{entity.name}` {over} without the definition's predicate ({predicate})",
            )
        )
    return out


def definition_period_check(ignored: Sequence[tuple[Definition, str]]) -> VerificationCheck:
    """The certified serve's ``definition_period`` check: pass when every
    period rule on the entities the approved SQL reads is applied or out of
    window; fail naming each term it reads past. Approval vouches for the
    SQL's meaning on the day it was written, not for a rule declared on a
    period it spans — the rule rides every rail, the certified one included."""
    if not ignored:
        return VerificationCheck(name="definition_period", status="pass")
    return VerificationCheck(
        name="definition_period",
        status="fail",
        reason="; ".join(f"`{d.term}` {why}" for d, why in ignored),
    )


@dataclass(frozen=True)
class ConservationPlan:
    """The two counts that decide ``row_conservation`` for one UNION ALL CTE
    over one source table: the rows the CTE yields, and the distinct declared
    keys of the table under the OR of every filter the branches apply."""

    cte: str
    entity: str
    keys: tuple[str, ...]
    produced_sql: str
    distinct_sql: str


def _union_branches(node: exp.Expression) -> list[exp.Expression] | None:
    """The leaves of a UNION ALL chain; None when any join in the chain is a
    distinct UNION (which cannot re-add a row) or a leaf is not a SELECT."""
    if isinstance(node, exp.Union):
        if node.args.get("distinct", True):
            return None
        left = _union_branches(cast(exp.Expression, node.left))
        right = _union_branches(cast(exp.Expression, node.right))
        return None if left is None or right is None else left + right
    return [node] if isinstance(node, exp.Select) else None


def _table_key(table: exp.Table) -> str:
    return ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()


def row_conservation_plan(
    sql: str, model: SemanticModel
) -> ConservationPlan | VerificationCheck | None:
    """Does the SQL carry the shape that double-counts — a UNION ALL CTE whose
    branches all read ONE source table and project the same columns (no
    literal tagging a branch)? None when no CTE has it. The plan when the
    entity declares a primary key; a SKIP naming the entity when it does not
    (the check cannot count keys it was never told). The generated
    de-duplication that kept May's single rows in one branch and re-added
    every May row in the other served 1.5x under `partial` with a generic
    reason; the shape is the tell, and counting is the proof."""
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:
        return None
    if stmt is None:
        return None
    with_clause = stmt.args.get("with_")
    if with_clause is None:
        return None
    ctes: dict[str, exp.Expression] = {
        cte.alias_or_name.lower(): cte.this for cte in with_clause.expressions
    }
    entities = {e.source.table.lower(): e for e in model.entities}

    def scans(node: exp.Expression, seen: set[str]) -> list[exp.Select] | None:
        """Every SELECT in *node* (CTE references followed) that reads a
        physical table directly; None when a table is not the model's."""
        out: list[exp.Select] = []
        for select in node.find_all(exp.Select):
            source = select.args.get("from_")
            if source is None or not isinstance(source.this, exp.Table):
                continue
            name = source.this.name.lower()
            if name in ctes and not source.this.db:
                if name in seen:
                    continue
                inner = scans(ctes[name], seen | {name})
                if inner is None:
                    return None
                out.extend(inner)
                continue
            out.append(select)
        return out

    for name, body in ctes.items():
        branches = _union_branches(body)
        if branches is None or len(branches) < 2:
            continue
        names = [tuple(e.alias_or_name.lower() for e in b.expressions) for b in branches]
        if len(set(names)) != 1 or any(
            isinstance(e.unalias(), exp.Literal) for b in branches for e in b.expressions
        ):
            continue
        read = scans(body, {name})
        if not read:
            continue
        tables = {_table_key(cast(exp.Table, s.args["from_"].this)) for s in read}
        if len(tables) != 1:
            continue
        table = tables.pop()
        entity = entities.get(table)
        if entity is None:
            continue
        if not entity.primary_key:
            return VerificationCheck(
                name="row_conservation",
                status="skip",
                reason=f"`{name}` unions rows of `{entity.name}` from one table, but the "
                "entity declares no primary_key to count them by",
            )
        filters = [
            _strip_refs(cast(exp.Expression, s.args["where"].this).copy())
            for s in read
            if s.args.get("where") is not None
        ]
        produced = exp.select(exp.func("COUNT", exp.Star())).from_(name)
        produced.set("with_", with_clause.copy())
        keyed = exp.select(*entity.primary_key).from_(table).distinct()
        if filters:
            keyed = keyed.where(exp.or_(*(exp.paren(f) for f in filters)))
        distinct = exp.select(exp.func("COUNT", exp.Star())).from_(keyed.subquery("k"))
        return ConservationPlan(
            cte=name,
            entity=entity.name,
            keys=tuple(entity.primary_key),
            produced_sql=produced.sql(dialect=model.dialect),
            distinct_sql=distinct.sql(dialect=model.dialect),
        )
    return None


def row_conservation_check(
    plan: ConservationPlan, produced: int, distinct: int
) -> VerificationCheck:
    """``row_conservation``: a union over one table may not yield more rows
    than the table has distinct keys under the same filters. Fails naming
    both counts — the reason `partial` carries."""
    if produced > distinct:
        return VerificationCheck(
            name="row_conservation",
            status="fail",
            reason=f"`{plan.cte}` yields {produced:,} rows from `{plan.entity}`, whose distinct "
            f"({', '.join(plan.keys)}) keys number {distinct:,} under the same filters — "
            "the union re-adds rows the other branch already kept",
        )
    return VerificationCheck(
        name="row_conservation",
        status="pass",
        reason=f"`{plan.cte}` yields {produced:,} rows over {distinct:,} distinct keys",
    )


def _population_declared(sql: str, model: SemanticModel) -> VerificationCheck:
    """Does the SQL apply the population bound of every scoped entity it reads?

    The metric compiler ANDs `population_filter` in mechanically, so this check
    guards the raw-SQL tier — the one place generation can walk past a declared
    bound. Relevance is by table/entity NAME in the SQL (word-boundary, raw
    text — the departure idiom: the SQL that misses a filter is often the SQL
    that half-parses). A fail caps at partial with the scope prose in the
    reason, so the caveat travels even when enforcement was missed."""
    scoped = [
        e
        for e in model.entities
        if (e.population_filter or "").strip()
        and (_touches(e.name, sql) or _touches(e.source.table.split(".")[-1], sql))
    ]
    if not scoped:
        return VerificationCheck(
            name="population_declared",
            status="skip",
            reason="no scoped entity (population_filter) in this query",
        )
    sql_n = _norm(sql)
    missing = [
        e
        for e in scoped
        if e.population_filter
        and not _expr_in_sql(e.population_filter, sql, sql_n, model)
        and not _predicate_in_sql(e.population_filter, sql, model.dialect)
    ]
    if not missing:
        return VerificationCheck(name="population_declared", status="pass")
    scopes = "; ".join(
        f"'{e.name}' holds only {e.population or e.population_filter}" for e in missing
    )
    return VerificationCheck(
        name="population_declared",
        status="fail",
        reason=(
            f"the query reads a scoped subset without its declared population filter — "
            f"{scopes} — the answer covers that subset, not the whole population"
        ),
    )


_AGG_FN = re.compile(r"\b(SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)


_UNDENOMINATED_TYPES = frozenset({"string", "boolean", "timestamp", "date", "json"})


def _undenominated_columns(model: SemanticModel) -> set[str]:
    """Columns the model declares as something no currency denominates — a
    date, a timestamp, text, a flag. ``MAX(cancelled_at)`` per customer is a
    churn date, not a total across currencies."""
    return {
        f.name.lower() for e in model.entities for f in e.fields if f.type in _UNDENOMINATED_TYPES
    }


def _denominating_aggregates(sql: str, model: SemanticModel) -> list[set[str]] | None:
    """The column sets of every aggregate whose result a pinned dimension
    denominates. COUNTs are left out (a row count is in no currency), so is
    a scalar subselect inside a predicate (the latest-snapshot pin's
    ``MAX(time_field)`` aggregates nothing the answer states), and so is an
    aggregate over columns the model declares as dates, timestamps, text or
    flags — a ``MAX(cancelled_at)`` or ``BOOL_AND(status = 'cancelled')``
    feeding a count. None when the SQL does not parse."""
    try:
        tree = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(tree, exp.Expression):
        return None
    undenominated = _undenominated_columns(model)
    out: list[set[str]] = []
    for agg in tree.find_all(exp.AggFunc):
        if isinstance(agg, exp.Count) or agg.find_ancestor(exp.Predicate) is not None:
            continue
        cols = {c.name.lower() for c in agg.find_all(exp.Column)}
        if cols and cols <= undenominated:
            continue
        out.append(cols)
    return out


def _declared_currency_columns(entity: Entity) -> set[str]:
    """Columns a metric of *entity* declares a currency for — a SUM over them
    is denominated by the author, not by a currency row dimension."""
    return {
        m.expr.rpartition(".")[2].lower()
        for m in entity.metrics
        if m.currency and m.expr and re.fullmatch(r"[\w.]+", m.expr)
    }


def _aggregation_scope(sql: str, model: SemanticModel) -> VerificationCheck:
    """Does an aggregate over an entity respect its pinned dimensions?
    (The structured half of prose like 'never sum across currencies'.)

    Coarse and false-positive-safe, the population_declared idiom: fires only
    when the SQL aggregates a VALUE (SUM/AVG/MIN/MAX — never a COUNT, which is
    denominated in nothing by nature), reads an entity with declared
    pinned_dimensions, and a pinned dimension appears NOWHERE in the
    statement — a SUM with no currency reference at all is a number
    denominated in nothing, and reads as if it were in the reader's own
    currency. Any mention (grouped, filtered, projected) passes; so does an
    aggregate over a column whose metric declares its currency (the author
    said what it is in); only total absence fails, toward partial, with the
    reason saying what the figure actually is."""
    if not _AGG_FN.search(sql):
        return VerificationCheck(
            name="aggregation_scope", status="skip", reason="no aggregate in this query"
        )
    scoped = [
        e
        for e in model.entities
        if e.pinned_dimensions
        and (_touches(e.name, sql) or _touches(e.source.table.split(".")[-1], sql))
    ]
    if not scoped:
        return VerificationCheck(
            name="aggregation_scope",
            status="skip",
            reason="no entity with pinned_dimensions in this query",
        )
    aggregates = _denominating_aggregates(sql, model)
    if aggregates is not None:
        if not aggregates:
            return VerificationCheck(
                name="aggregation_scope",
                status="skip",
                reason="only counts (or a snapshot pin) aggregate here — nothing denominated",
            )
        declared = set().union(*(_declared_currency_columns(e) for e in scoped))
        if declared and all(cols and cols <= declared for cols in aggregates):
            return VerificationCheck(
                name="aggregation_scope",
                status="pass",
                reason="every aggregate is over a column whose metric declares its currency",
            )
    missing = [
        (e.name, d) for e in scoped for d in e.pinned_dimensions if d and not _touches(d, sql)
    ]
    if not missing:
        return VerificationCheck(name="aggregation_scope", status="pass")
    dims = "; ".join(f"'{d}' ({e})" for e, d in missing)
    return VerificationCheck(
        name="aggregation_scope",
        status="fail",
        reason=(
            f"an aggregate over a pinned dimension it never references — {dims} must "
            "be filtered to one value or grouped before summing; a total across it "
            "is a number denominated in nothing"
        ),
    )


def _filtered_columns(sql: str, dialect: str | None) -> set[str]:
    """Column names appearing under any WHERE clause (subqueries included) —
    the columns whose predicates decide which rows exist. An unparseable query
    returns the empty set: the caller must skip, never guess."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return set()
    if not isinstance(tree, exp.Expression):
        return set()
    return {c.name.lower() for w in tree.find_all(exp.Where) for c in w.find_all(exp.Column)}


def date_coverage_check(
    question: str,
    sql: str,
    model: SemanticModel,
    coverage: Mapping[str, EntityCoverage] | None,
    *,
    profile_stale: bool = False,
) -> VerificationCheck:
    """The asked period vs the tables' MEASURED date span — `freshness_check`'s
    sibling on the same declared-clock rail. A question about a period the
    warehouse never loaded produces a confident zero: the aggregate runs, the
    number grounds, and nothing says the rows could not exist. Deterministic by
    construction, false positives forbidden: it fires only when a window that
    RESOLVES to calendar dates lies ENTIRELY outside the whole-table MIN/MAX of
    a time column the SQL actually filters on, and the "data begins" direction
    additionally requires the measured column to be the entity's own declared
    time field (a load stamp's MIN says when loading started, not where history
    does). Anything unmeasurable or ambiguous is a skip with its reason — a
    profile past ``probe.max_age`` included: its span may end where the
    loading has long since moved on."""
    if coverage and profile_stale:
        return VerificationCheck(name="date_coverage", status="skip", reason=STALE_REASON)
    if not coverage:
        return VerificationCheck(
            name="date_coverage",
            status="skip",
            reason="date coverage not measured — the scope's tables have no probed time span",
        )
    terms = temporal_terms(question)
    if not terms:
        return VerificationCheck(
            name="date_coverage", status="skip", reason="the question states no time window"
        )
    ranges = window_ranges(terms, _lens_today(model.timezone))
    if not ranges:
        return VerificationCheck(
            name="date_coverage",
            status="skip",
            reason="the stated window does not resolve to calendar dates",
        )
    filtered = _filtered_columns(sql, model.dialect)
    problems: list[str] = []
    dimensions: list[str] = []
    for name, cov in sorted(coverage.items()):
        if cov.column.lower() not in filtered:
            # Nothing pinned rows to this table's clock — its span cannot have
            # zero-filled the result (a customers.created_at ending years ago
            # must not flag a revenue question that filters invoice_date).
            continue
        table = cov.table.split(".")[-1]
        if not re.search(rf"\b{re.escape(name)}\b|\b{re.escape(table)}\b", sql, re.IGNORECASE):
            continue
        if not cov.time_series:
            # A dimension's date is when its members arrived, not a loaded
            # period: nothing here can prove the asked window was never loaded.
            dimensions.append(f"{name} ({cov.column})")
            continue
        if all(start > cov.end for start, _ in ranges):
            if cov.snapshot:
                problems.append(
                    f"{name} is a single snapshot as of {cov.end.isoformat()} ({cov.column})"
                )
            else:
                problems.append(f"{name} data ends {cov.end.isoformat()} ({cov.column})")
        elif cov.declared and all(end < cov.start for _, end in ranges):
            problems.append(f"{name} data begins {cov.start.isoformat()} ({cov.column})")
    if problems:
        stated = ", ".join(sorted(terms))
        return VerificationCheck(
            name="date_coverage",
            status="fail",
            reason=(
                f"the question asks about '{stated}' but "
                + "; ".join(problems)
                + " — an empty or zero result here means the period was never "
                "loaded, not that nothing happened"
            ),
        )
    if dimensions:
        return VerificationCheck(
            name="date_coverage",
            status="skip",
            reason=(
                f"{', '.join(dimensions)} declares no default_time_field — a dimension's "
                "date span says when members were added, not which periods were loaded"
            ),
        )
    return VerificationCheck(name="date_coverage", status="pass")


def freshness_check(
    data_as_of: str | None,
    model: SemanticModel,
    *,
    profile_stale: bool = False,
    table: str | None = None,
) -> VerificationCheck:
    """The declared freshness contract vs the MEASURED freshness of what the
    SQL read — a plain date comparison, deterministic by construction: false
    positives are forbidden, so anything unmeasurable is a skip with its
    reason, never a fail and never a vacuous pass. ``data_as_of`` is the
    pipeline's read-set date (the oldest time-series table the SQL read, a
    dimension only when nothing else was), never the lens-wide floor; ``table``
    names it in the reason when known. A measurement older than
    ``probe.max_age`` is unmeasurable too: it dates the warehouse to the last
    probe, not to now."""
    days = model.stale_after_days
    if not days:
        return VerificationCheck(
            name="freshness", status="skip", reason="lens declares no stale_after_days"
        )
    if data_as_of and profile_stale:
        return VerificationCheck(name="freshness", status="skip", reason=STALE_REASON)
    if not data_as_of:
        return VerificationCheck(
            name="freshness",
            status="skip",
            reason="freshness not measured — the scope's tables have no profile",
        )
    try:
        as_of = date.fromisoformat(data_as_of)
    except ValueError:
        return VerificationCheck(
            name="freshness",
            status="skip",
            reason=f"measured freshness '{data_as_of}' is not an ISO date",
        )
    age = (_lens_today(model.timezone) - as_of).days
    if age > days:
        return VerificationCheck(
            name="freshness",
            status="fail",
            reason=(
                f"data{f' in {table}' if table else ''} is as of {data_as_of} ({age} days old)"
                f" — this lens declares data stale after {days} days"
            ),
        )
    return VerificationCheck(name="freshness", status="pass", reason=None)


def clock_years(timezone: str | None) -> tuple[int, int]:
    """The lens clock's current and previous year — grading-time is close enough
    to serve-time that the pair also absorbs a New-Year boundary."""
    try:
        now = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now(UTC)
    except Exception:
        now = datetime.now(UTC)
    return (now.year, now.year - 1)


def _printed_decimals(want: float) -> int:
    """How many decimals the stored value prints with: 82.59 -> 2, 7 -> 0. The
    certified value is the stated precision; a served value that rounds to it
    at that precision has not moved."""
    if isinstance(want, int):
        return 0
    text = repr(float(want))
    if "e" in text or "." not in text:
        return 0
    return len(text.split(".")[1].rstrip("0"))


def _as_number(v: object) -> decimal.Decimal | None:
    """A cell as a number when it is one. The store writes Decimal cells as
    text, so a numeric-looking string counts; bools never do."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float | decimal.Decimal):
        return decimal.Decimal(str(v))
    if isinstance(v, str):
        try:
            return decimal.Decimal(v.strip())
        except (decimal.InvalidOperation, ValueError):
            return None
    return None


def _cell_moved(want: object, got: object) -> bool:
    """Did one cell move beyond the stored value's printed precision? Numbers
    compare numerically at that precision; everything else compares through
    the same canonical reading the eval gate uses (``canon_cell``), so a
    timestamp's two text forms, a date against its midnight timestamp, or a
    ``Decimal`` against the float it was stored as never read as a move."""
    w, g = _as_number(want), _as_number(got)
    if w is not None and g is not None:
        decimals = _printed_decimals(float(w) if not isinstance(want, int) else want)
        return round(float(g), decimals) != round(float(w), decimals)
    return bool(canon_cell(want) != canon_cell(got))


def _cell_text(v: object) -> str:
    return "null" if v is None else str(v)


def restatement(
    verified_value: Mapping[str, object], result: QueryResult
) -> tuple[str, str] | None:
    """``(old, new)`` when the executed result differs from the value stored at
    certify time beyond its printed precision; None when it stands. A scalar
    summary (``{"value": x}``) compares the one cell; a table summary compares
    the stored rows (the store keeps a capped sample, so a longer result is
    compared on its first rows) cell by cell, order-insensitively."""
    if "value" in verified_value:
        cell = result.rows[0][0] if result.rows and result.rows[0] else None
        old = verified_value["value"]
        if len(result.rows) != 1 or len(result.columns) != 1 or _cell_moved(old, cell):
            new = (
                _cell_text(cell)
                if len(result.rows) == 1 and len(result.columns) == 1
                else f"a {len(result.rows)}-row, {len(result.columns)}-column result"
            )
            return _cell_text(old), new
        return None
    stored_rows = verified_value.get("rows")
    if not isinstance(stored_rows, list):
        return None
    # The full count, when the probe fetched the whole result: a result that
    # gained or lost rows has moved even when its first rows stand.
    stored_count = verified_value.get("row_count")
    if isinstance(stored_count, int) and stored_count != len(result.rows):
        return f"{stored_count} rows", f"{len(result.rows)} rows"
    live = [list(r) for r in result.rows[: len(stored_rows)]]
    if len(live) != len(stored_rows):
        return f"{len(stored_rows)} rows", f"{len(result.rows)} rows"
    key = lambda row: [_cell_text(c) for c in row]  # noqa: E731
    for want, got in zip(sorted(stored_rows, key=key), sorted(live, key=key), strict=True):
        if len(want) != len(got):
            return f"{len(want)} columns", f"{len(got)} columns"
        for i, (w, g) in enumerate(zip(want, got, strict=True)):
            if _cell_moved(w, g):
                col = result.columns[i] if i < len(result.columns) else f"column {i + 1}"
                return f"{col} {_cell_text(w)}", f"{col} {_cell_text(g)}"
    return None


def prose_sampled(verified_value: Mapping[str, object] | None, result: QueryResult) -> str | None:
    """Why the certify-time prose must NOT serve verbatim over this result: it
    was composed over a stored SAMPLE shorter than the result (an answer
    certified before the probe fetched the whole result records no
    ``row_count``), so its sentences describe part of what the rows say.
    None when the prose covers the result — a scalar, a full row count on
    record, or a sample as long as the result."""
    if not verified_value or "value" in verified_value or "row_count" in verified_value:
        return None
    stored_rows = verified_value.get("rows")
    if not isinstance(stored_rows, list) or len(stored_rows) >= len(result.rows):
        return None
    return (
        f"the certify-time prose was composed over a {len(stored_rows)}-row sample of a "
        f"{len(result.rows)}-row result"
    )


def restated_check(
    verified_value: Mapping[str, object] | None,
    result: QueryResult,
    *,
    skip_reason: str | None = None,
) -> tuple[VerificationCheck, tuple[str, str] | None]:
    """The ``restated`` check for a certified serve: does the executed result
    still say what it said when a human verified it? A fail caps the grade at
    partial and keeps the certify-time prose off the wire. No stored value is
    a SKIP with its reason, never a pass — the pass has to be earned."""
    if skip_reason is not None:
        return VerificationCheck(name="restated", status="skip", reason=skip_reason), None
    if not verified_value:
        return (
            VerificationCheck(
                name="restated",
                status="skip",
                reason="no verified value was stored when this answer was certified",
            ),
            None,
        )
    if "error" in verified_value:
        return (
            VerificationCheck(
                name="restated",
                status="skip",
                reason="the certify-time execution errored, so there is no value to compare",
            ),
            None,
        )
    moved = restatement(verified_value, result)
    if moved is None:
        return VerificationCheck(name="restated", status="pass"), None
    old, new = moved
    return (
        VerificationCheck(
            name="restated",
            status="fail",
            reason=f"the result moved from {old} to {new} since this answer was verified",
        ),
        moved,
    )


def build_report(
    *,
    question: str,
    sql: str,
    answer_text: str,
    result: QueryResult,
    semantic_model: SemanticModel,
    definition_used: str | None,
    truncated: bool,
    certification: str,
    row_count_exact: bool = True,
    composed: bool = True,
    uncomposed_reason: str | None = None,
    investigation: Literal["confirmed", "unresolved"] | None = None,
    data_as_of: str | None = None,
    # The table ``data_as_of`` dates (the pipeline's scope_as_of), named in
    # the freshness reason so the check and the answer stamp agree.
    as_of_table: str | None = None,
    entity_coverage: Mapping[str, EntityCoverage] | None = None,
    profile_stale: bool = False,
    # The governed metrics a compiled intent named, when the SQL was compiled
    # from the model (sole-metric door, typed serving) rather than generated.
    compiled_metrics: Sequence[str] = (),
    # A certified serve's ``restated`` check (restated_check), appended as built.
    restated: VerificationCheck | None = None,
    # The approved question behind a certified serve: window_applied passes on
    # approved SQL only when it states the asked window.
    certified_question: str | None = None,
    # A certified serve's ``definition_period`` check (definition_period_check).
    definition_period: VerificationCheck | None = None,
    # The generated path's ``row_conservation`` check (row_conservation_check).
    row_conservation: VerificationCheck | None = None,
) -> VerificationReport:
    numeric_status: CheckStatus
    if composed:
        # The applied definition's body grounds a rate it states (e.g. "default 10%"),
        # and the question grounds numbers the answer merely echoes from the ask ("more
        # than 3 orders") — neither is an invented number.
        # The checker sees WHAT THE COMPOSER SAW: when the answer is computed
        # from a METRIC, `definition_used` holds the metric name, the term
        # lookup misses, and definition prose the composer was legitimately
        # quoting ("a 14-day rolling window") gets flagged as an invented
        # figure. Same source as the compose prompt:
        # governing_definitions over the result's columns plus whatever the
        # generator says it applied.
        from services.runtime.answer import governing_definitions

        governing = governing_definitions(semantic_model, result.columns, definition_used)
        definition_prose = "\n".join(d.body for d in governing)
        numeric_status, numeric_reason = faithfulness.numeric_check(
            answer_text,
            result,
            definition_text=definition_prose,
            question_text=question,
            # Declared profile facts about the projected columns are groundable —
            # the composer is handed them with an attribution rule; flagging a
            # restated profile stat as invented is the dominant unverified
            # false positive. So is the declared population the composer is
            # told to state: a scope said in words carries its year.
            notes_text=grounding_notes(semantic_model, result.columns, sql),
            # …and so are the served SQL's own literals (the WHERE clause's year
            # is provenance the caller can read in the trace, not an invention),
            # and the lens clock's current/previous year — "this year" resolved
            # against CURRENT_DATE leaves no literal for the prose's honest
            # "in 2026" to ground against.
            sql_text=sql,
            dialect=semantic_model.dialect,
            clock_years=clock_years(semantic_model.timezone),
            # Truncation is context the check must hold: a claim that cannot
            # ground over a SHORT window is the model filling the gap the cap
            # left, and the soft skips are withdrawn there.
            truncated=truncated,
        )
    else:
        # No FREE prose was written, so there is no invented number for the check to
        # catch. Two shapes share the skip: a rows-only serve (format=structured), and
        # the certified serves — stored prose grounded once at certify time, or
        # the deterministic frame written by code — where ``uncomposed_reason`` names
        # which. It did not RUN — that is a skip, and this one must not cost the grade
        # the way a numberless answer's skip does.
        numeric_status = "skip"
        numeric_reason = uncomposed_reason or "no prose was composed (format=structured)"
    # A row of nothing is not evidence: an aggregate over zero matching rows
    # returns ONE all-NULL row, which row-presence alone grades "verified".
    # Only rows with at least one non-NULL value count.
    has_rows = any(any(v is not None for v in row) for row in result.rows)
    checks = [
        VerificationCheck(name="numeric_grounding", status=numeric_status, reason=numeric_reason),
        _definition_applied(
            question, semantic_model, definition_used, sql, certification, compiled_metrics
        ),
        _intent_alignment(question, sql, semantic_model.dialect),
        _window_applied(question, sql, semantic_model, certification, certified_question),
        _level_grain(sql, semantic_model, certification, question),
        time_guard.grain_applied(question, result.columns, result.rows, certification),
        _zero_ratio_plausibility(sql, result),
        _all_ones_ratio(sql, result),
        _ratio_population(sql, semantic_model.dialect),
        _population_declared(sql, semantic_model),
        _aggregation_scope(sql, semantic_model),
        freshness_check(data_as_of, semantic_model, profile_stale=profile_stale, table=as_of_table),
        date_coverage_check(
            question, sql, semantic_model, entity_coverage, profile_stale=profile_stale
        ),
        VerificationCheck(
            name="has_rows",
            status="pass" if has_rows else "fail",
            reason=None
            if has_rows
            else (
                "the query returned only NULL values (an aggregate over zero matching rows)"
                if result.rows
                else "the query returned no rows"
            ),
        ),
        VerificationCheck(
            name="not_truncated",
            status="fail" if truncated else "pass",
            reason=(
                "the response carries fewer than the query's "
                f"{'more than ' if not row_count_exact else ''}{len(result.rows)} result rows"
                if truncated
                else None
            ),
        ),
    ]
    # The pipeline's zero-evidence investigation (value_guard): a confirmed zero
    # is evidence — the filtered values exist, the empty result is the data's
    # answer. An unresolved one is a zero NOBODY could check, and `has_rows`
    # alone cannot see the one-row aggregate costume (COUNT → 0 passes it) — the
    # fail is what keeps that serve out of `verified` and inside auto_review's
    # "partial" net.
    if investigation is not None:
        checks.append(
            VerificationCheck(
                name="empty_result_investigation",
                status="pass" if investigation == "confirmed" else "fail",
                reason=(
                    "the filtered values exist in the warehouse — the empty result is "
                    "the data's answer"
                    if investigation == "confirmed"
                    else "the filtered columns did not enumerate, so nothing confirms "
                    "the filter values exist — treat the zero as unchecked"
                ),
            )
        )
    if restated is not None:
        checks.append(restated)
    if definition_period is not None:
        checks.append(definition_period)
    if row_conservation is not None:
        checks.append(row_conservation)
    return _with_grade(checks, certification, numeric_status, composed=composed)


# Checks whose fail is an opinion, never the cause of a grade: the judge is
# advisory (fold_judge), so partial_reason never names it.
_ADVISORY = frozenset({"judge"})

# The checks a certified serve's grade does not read: the judge (advisory
# everywhere) and definition_applied, which abstains on certified SQL by
# contract — the SQL is approved, not generated from the definition.
CERTIFIED_WAIVED = _ADVISORY | {"definition_applied"}


def partial_reason(grade: Grade, checks: list[VerificationCheck]) -> str | None:
    """Why the grade is `partial`, named — never a bare word. The first
    deterministic fail, else the numeric skip, else the semantic abstention;
    None for any other grade (unverified names its fail on the check; verified
    needs no excuse)."""
    if grade != "partial":
        return None
    for c in checks:
        if c.status == "fail" and c.name not in _ADVISORY:
            return f"{c.name}: {c.reason}" if c.reason else c.name
    numeric = next((c for c in checks if c.name == "numeric_grounding"), None)
    if numeric is not None and numeric.status == "skip":
        return f"numeric_grounding skipped: {numeric.reason or 'no numeric claims to ground'}"
    if _semantic_abstained(checks):
        reasons = "; ".join(
            f"{c.name}: {c.reason}"
            for c in checks
            if c.name in ("definition_applied", "intent_alignment") and c.reason
        )
        return f"no semantic check confirmed the answer{f' ({reasons})' if reasons else ''}"
    return "graded on partial evidence"


def _report(grade: Grade, checks: list[VerificationCheck]) -> VerificationReport:
    return VerificationReport(
        grade=grade, checks=checks, partial_reason=partial_reason(grade, checks)
    )


def fold_ungoverned(report: VerificationReport, reason: str) -> VerificationReport:
    """The serve_ungoverned_shapes knob's promise, enforced: a serve that only
    happened because the knob bypassed the selection boundary grades unverified,
    deterministically. Without this fold the promise is only a docstring, and a
    pinning test for it can pass by accident — on some unrelated check failing
    — which makes it vacuous: it stops holding the moment that check improves."""
    checks = [
        *report.checks,
        VerificationCheck(name="governed_shape", status="fail", reason=reason),
    ]
    return _report("unverified", checks)


def fold_exposed(report: VerificationReport, tables: list[str]) -> VerificationReport:
    """An answer read from a table the lens exposes without governed
    definitions caps at partial, deterministically. The EXPOSED disclosure
    line said so on the surface while the grade still read verified — a
    badge vouching for columns nobody defined. The failing check names the
    forcing reason, so `partial` is never a bare word."""
    if not tables:
        return report
    what = "a table" if len(tables) == 1 else "tables"
    checks = [
        *report.checks,
        VerificationCheck(
            name="governed_read",
            status="fail",
            reason=f"ungoverned read: {', '.join(tables)}, {what} with no governed definitions",
        ),
    ]
    grade: Grade = "partial" if report.grade == "verified" else report.grade
    return _report(grade, checks)


def fold_metric_filter_waived(report: VerificationReport, reason: str) -> VerificationReport:
    """The filter-guard waiver, recorded where nothing can lose it: the served
    SQL computes an aggregation shape a governed metric claims, WITHOUT that
    metric's mandatory filter — waived because the question named no metric of
    the group (filter_guard's question rule), so the figure is a plain
    ungoverned aggregate, not the metric. The serve is the point (the
    alternative was a dead-end rejection of a trivially answerable question),
    but question↔metric alignment is exactly what nothing deterministic can
    confirm here: the check records the miss and the grade caps at partial —
    inside auto_review's net, where the authoring gap behind the waiver (the
    missing unfiltered sibling metric) becomes visible instead of silent."""
    checks = [
        *report.checks,
        VerificationCheck(name="metric_filter_waived", status="fail", reason=reason),
    ]
    grade: Grade = "partial" if report.grade == "verified" else report.grade
    return _report(grade, checks)


def fold_low_confidence(
    report: VerificationReport, low: list[tuple[DecisionRecord, float]]
) -> VerificationReport:
    """An acted decision below its slot's measured bar, folded in as one more
    check: the slot, the choice and the probability are named, and the grade
    caps at partial — disclosed, never refused (decision_policy: the typed
    regime acts unless none wins)."""
    from services.runtime import decision_policy

    checks = [
        *report.checks,
        VerificationCheck(
            name="decision_confidence", status="fail", reason=decision_policy.describe_low(low)
        ),
    ]
    grade: Grade = "partial" if report.grade == "verified" else report.grade
    return _report(grade, checks)


def fold_judge(report: VerificationReport, verdict: str, reasoning: str) -> VerificationReport:
    """Fold an inline judge verdict into the report as one more check — ADVISORY.

    The grade comes from the deterministic checks and the judge never moves
    it: the same SQL graded three ways across repeats when a model's opinion
    could demote, and a lens's own declared population read as "unsupported"
    to a judge that had not been shown it. approve → pass; changes/reject →
    fail, visible as the judge's note on the report, with the grade untouched."""
    status: CheckStatus = "pass" if verdict == "approve" else "fail"
    checks = [
        *report.checks,
        VerificationCheck(
            name="judge",
            status=status,
            reason=f"advisory ({verdict}): {reasoning}" if reasoning else f"advisory ({verdict})",
        ),
    ]
    return _report(report.grade, checks)


def fold_challenges(
    report: VerificationReport, challenges: list[adversary.Challenge] | None
) -> VerificationReport:
    """Fold the adversarial reviewer's challenges into the report as one
    more check.

    A blocking challenge fails the check and caps the grade at partial — a raised,
    unresolved challenge means "not verified", but the adversary's opinion alone
    never destroys deterministic evidence (mirror of fold_judge's 'changes').
    Notes are surfaced in the reason without demoting; the grade never upgrades.
    ``None`` is a reviewer whose reply could not be parsed: that carries no
    signal about the answer, so the check is a SKIP that says so — never a
    pass dressed as a note."""
    if challenges is None:
        checks = [
            *report.checks,
            VerificationCheck(
                name="adversary",
                status="skip",
                reason="the adversary's reply could not be parsed; no challenges extracted",
            ),
        ]
        return _report(report.grade, checks)
    blocking = [c for c in challenges if c.severity == "blocking"]
    detail = "; ".join(f"{c.assumption}: {c.reason}" for c in challenges)
    status: CheckStatus = "fail" if blocking else "pass"
    checks = [
        *report.checks,
        VerificationCheck(
            name="adversary",
            status=status,
            reason=detail or "no challenges raised",
        ),
    ]
    grade: Grade = report.grade
    if blocking and grade == "verified":
        grade = "partial"
    return _report(grade, checks)


def _with_grade(
    checks: list[VerificationCheck],
    certification: str,
    numeric_status: str,
    *,
    composed: bool = True,
) -> VerificationReport:
    fails = {c.name for c in checks if c.status == "fail"}
    grade: Grade
    if certification == "certified":
        # Certified is the top tier: the SQL is human-approved, so the checks
        # that ask whether GENERATION honoured the model are waived
        # (`CERTIFIED_WAIVED`: definition_applied abstains by contract, the
        # judge is advisory). Everything else still binds, and a fail demotes:
        # prose that contradicts the approved result, a payload served short,
        # data past the freshness contract, a period the warehouse never
        # loaded, a shape or scope the serve got wrong. Approval vouches for
        # the SQL's meaning, never for what happened around it — "verified"
        # over a failed check is the badge vouching for a miss.
        grade = "partial" if fails - CERTIFIED_WAIVED else "verified"
    elif fails & {"numeric_grounding", "intent_alignment"}:
        grade = "unverified"
    elif fails or (composed and numeric_status == "skip") or _semantic_abstained(checks):
        # No numeric claims means nothing was verifiable — honest "partial", never an
        # inflated pass. Uncomposed is a different skip (there was no prose to check at
        # all) and does not demote: the deterministic checks still stand on their own.
        grade = "partial"
    else:
        grade = "verified"
    return _report(grade, checks)


def _semantic_abstained(checks: list[VerificationCheck]) -> bool:
    """`verified` requires at least one SUBSTANTIVE semantic pass: grading from
    the absence of failures lets a serve where every semantic check abstained
    ("skip") earn the same badge as a fully confirmed one — on a prose-only
    lens, `verified` quietly reduces to "the numbers appear in a non-empty
    result". Skips stay the right outcome for the checks
    themselves (their docstrings promise fail-open-to-skip); the grade just
    stops reading "I could not test this" as evidence.

    One pass is enough on purpose: definition_applied's "not relevant" skip is
    a real verdict (pinned by the boundary tests — an unrelated question must
    not fail it), and an answer can legitimately grade verified on an applied
    definition while intent abstains. Only the serve where NOTHING
    semantic confirmed loses the badge; payload checks (has_rows /
    not_truncated / freshness / date_coverage) are structural and never count either way.
    Every skip's reason stays visible on the checks array the caller receives."""
    semantic = [c for c in checks if c.name in ("definition_applied", "intent_alignment")]
    # A list with no semantic checks at all is a synthetic/partial one (tests,
    # folds) — build_report always carries both, so only a real report where
    # they ALL abstained loses the badge.
    return bool(semantic) and not any(c.status == "pass" for c in semantic)
