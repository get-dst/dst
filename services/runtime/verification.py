"""Build the graded verification report from named, deterministic checks.

Replaces the binary high/low confidence: each check passes/fails/skips with a reason,
and the headline grade is derived from the breakdown — never the reverse. The checks
target the two real failure classes: invented numbers (numeric_grounding) and
wrong-but-valid SQL (definition_applied, intent_alignment).
"""

from __future__ import annotations

import decimal
import re
from datetime import UTC, date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import expressions as exp

from services.contracts.semantic_model import SemanticModel
from services.contracts.verification import (
    CheckStatus,
    Grade,
    VerificationCheck,
    VerificationReport,
)
from services.contracts.warehouse import QueryResult
from services.runtime import adversary, faithfulness, sql_guard
from services.runtime.answer import data_notes as answer_data_notes

_DISTINCT_ASK = re.compile(r"\b(unique|distinct|individual|deduplicated)\b", re.IGNORECASE)
_COUNT_ASK = re.compile(r"\b(how many|count|number of)\b", re.IGNORECASE)


def _norm(sql_fragment: str) -> str:
    # Whitespace-insensitive: a definition's sql_expr is "in" the SQL whether the
    # generator wrote `SUM(x) * 0.1` or `SUM( x )*0.1`. Dropping all whitespace makes
    # the containment check robust to formatting, the dominant cause of false fails.
    return re.sub(r"\s+", "", sql_fragment).lower()


def _unqualify(sql_text: str) -> str | None:
    """Normalised SQL with column qualifiers (catalog/db/table) stripped, so a
    definition whose sql_expr names a column fully-qualified
    (``DB.SCHEMA.TBL.COL * 0.10``) still matches the same column referenced bare
    (``COL * 0.10``) in the executed SQL. None if the fragment doesn't parse.

    Single-precision casts are widened on both sides for the same reason: the guard
    now rewrites ``CAST(x AS REAL)`` to ``CAST(x AS DOUBLE)`` before serving, so a
    definition authored with the narrow cast would otherwise read as *not applied* —
    a precision fix silently costing the grade it was meant to protect.

    Identifier QUOTING is stripped for the third time in the same story: a serve
    that applied the definition exactly is graded as departing from it when the
    only difference is that generation wrote ``c."status_code" = 'S0121'`` instead
    of ``c.status_code = 'S0121'``. Nothing here executes — this string is
    only ever an argument to ``in`` — and ``_norm`` already lowercases both sides,
    so the case-sensitivity that quoting buys is gone before it could matter."""
    try:
        tree = sqlglot.parse_one(sql_text)
    except Exception:
        return None
    if tree is None:
        return None
    for col in tree.find_all(exp.Column):
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    for ident in tree.find_all(exp.Identifier):
        ident.set("quoted", False)
    sql_guard.widen_single_precision(tree)
    return _norm(tree.sql())


def _expr_in_sql(expr: str, sql: str, sql_norm: str) -> bool:
    """Is the definition's expression applied in the SQL? Literal match first
    (cheap, exact); then a qualifier-insensitive parse, so a table-qualified
    sql_expr still matches the column used bare in the executed SQL — the bug that
    made every commission answer fail definition_applied despite applying it."""
    if _norm(expr) in sql_norm:
        return True
    unqualified_expr, unqualified_sql = _unqualify(expr), _unqualify(sql)
    return (
        unqualified_expr is not None
        and unqualified_sql is not None
        and unqualified_expr in unqualified_sql
    )


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
        if _expr_in_sql(expr, sql, sql_norm) or _about_referenced(d.about, sql):
            continue
        departed.append(d.term)
    return departed


def _definition_applied(
    question: str,
    semantic_model: SemanticModel,
    definition_used: str | None,
    sql: str,
    certification: str = "none",
) -> VerificationCheck:
    """A definition is *applied* only when its sql_expr is in the executed SQL —
    'a definition exists' is exactly the signal the binary badge over-trusted.

    A certified serve abstains instead of failing. This check asks whether
    GENERATION honoured a definition, and a certified answer is not generated:
    its SQL was written and approved by a person, and serving it runs no model
    at all. "fail" would assert a departure that cannot have happened, and the
    grade already carves certified out for exactly this reason (see
    ``_with_grade``: only numeric_grounding, not_truncated and freshness demote
    it). Abstaining keeps the two layers saying the same thing, and keeps the
    honest reading — the definition was not checked here, which is not the same
    as checked and clean."""
    if certification == "certified":
        return VerificationCheck(
            name="definition_applied",
            status="skip",
            reason="certified: the SQL is human-approved, not generated from the definition",
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
        if expr is not None and _expr_in_sql(expr, sql, sql_n):
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
        expr is not None and _expr_in_sql(expr, sql, sql_n) for expr in enforceable.values()
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


def _window_applied(question: str, sql: str, model: SemanticModel) -> VerificationCheck:
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
    from services.runtime.timewindow import sql_engages_time, temporal_terms

    terms = temporal_terms(question)
    if not terms:
        return VerificationCheck(
            name="window_applied", status="skip", reason="the question states no time window"
        )
    time_fields = {e.default_time_field for e in model.entities if e.default_time_field}
    time_fields |= {
        f.name
        for e in model.entities
        for f in e.fields
        if "date" in (f.type or "").lower() or "time" in (f.type or "").lower()
    }
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
            for g in ("week", "month", "quarter", "year", "q1", "q2", "q3", "q4")
        )
        from services.runtime.timewindow import _SQL_DATE_EVIDENCE

        if (
            period_ask
            and not _SQL_DATE_EVIDENCE.search(sql)
            and _snapshot_pin_only(sql, model.dialect, time_fields)
        ):
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
    rows divided by COUNT(DISTINCT rep) over rows WHERE quota > 0 served
    200,849 where the answer is 16,411 — a figure that is not a valid measure
    of anything, identical across model tiers.

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


def _strip_refs(node: exp.Expression) -> exp.Expression:
    """Qualifiers and identifier quoting off a parsed expression, in place —
    the `_unqualify` treatment for predicate comparison."""
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
        and not _expr_in_sql(e.population_filter, sql, sql_n)
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


def _aggregation_scope(sql: str, model: SemanticModel) -> VerificationCheck:
    """Does an aggregate over an entity respect its pinned dimensions?
    (The structured half of prose like 'never sum across currencies'.)

    Coarse and false-positive-safe, the population_declared idiom: fires only
    when the SQL aggregates, reads an entity with declared pinned_dimensions,
    and a pinned dimension appears NOWHERE in the statement — a SUM with no
    currency reference at all is a number denominated in nothing, and reads
    as if it were in the reader's own currency. Any mention
    (grouped, filtered, projected) passes; only total absence fails, toward
    partial, with the reason saying what the figure actually is."""
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


def freshness_check(data_as_of: str | None, model: SemanticModel) -> VerificationCheck:
    """The declared freshness contract vs the MEASURED scope freshness — a plain
    date comparison, deterministic by construction: false positives are
    forbidden, so anything unmeasurable is a skip with its reason, never a fail
    and never a vacuous pass."""
    days = model.stale_after_days
    if not days:
        return VerificationCheck(
            name="freshness", status="skip", reason="lens declares no stale_after_days"
        )
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
                f"data is as of {data_as_of} ({age} days old) — this lens declares "
                f"data stale after {days} days"
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
            # false positive.
            notes_text=answer_data_notes(semantic_model, result.columns),
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
        _definition_applied(question, semantic_model, definition_used, sql, certification),
        _intent_alignment(question, sql, semantic_model.dialect),
        _window_applied(question, sql, semantic_model),
        _zero_ratio_plausibility(sql, result),
        _ratio_population(sql, semantic_model.dialect),
        _population_declared(sql, semantic_model),
        _aggregation_scope(sql, semantic_model),
        freshness_check(data_as_of, semantic_model),
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
    return _with_grade(checks, certification, numeric_status, composed=composed)


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
    return VerificationReport(grade="unverified", checks=checks)


def fold_judge(report: VerificationReport, verdict: str, reasoning: str) -> VerificationReport:
    """Fold an inline judge verdict into the report as one more check.

    approve → pass (the grade never upgrades past what the deterministic checks
    earned); reject → unverified; changes → capped at partial."""
    status: CheckStatus = "pass" if verdict == "approve" else "fail"
    checks = [
        *report.checks,
        VerificationCheck(name="judge", status=status, reason=reasoning or verdict),
    ]
    grade: Grade = report.grade
    if verdict == "reject":
        grade = "unverified"
    elif verdict == "changes" and grade == "verified":
        grade = "partial"
    return VerificationReport(grade=grade, checks=checks)


def fold_challenges(
    report: VerificationReport, challenges: list[adversary.Challenge]
) -> VerificationReport:
    """Fold the adversarial reviewer's challenges into the report as one
    more check.

    A blocking challenge fails the check and caps the grade at partial — a raised,
    unresolved challenge means "not verified", but the adversary's opinion alone
    never destroys deterministic evidence (mirror of fold_judge's 'changes').
    Notes are surfaced in the reason without demoting; the grade never upgrades."""
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
    return VerificationReport(grade=grade, checks=checks)


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
        # Certified is the top tier: the SQL is human-approved. Only prose that
        # contradicts the (approved) result — or a payload that doesn't carry all
        # of it — can demote it. Approved SQL served short is still a short answer:
        # "verified" over a truncated payload is the certification badge vouching
        # for rows the caller never received. Stale is the
        # third demotion: approval vouches for the SQL, not for month-old data —
        # a certified serve past the declared freshness contract is still stale.
        grade = (
            "partial" if fails & {"numeric_grounding", "not_truncated", "freshness"} else "verified"
        )
    elif fails & {"numeric_grounding", "intent_alignment"}:
        grade = "unverified"
    elif fails or (composed and numeric_status == "skip") or _semantic_abstained(checks):
        # No numeric claims means nothing was verifiable — honest "partial", never an
        # inflated pass. Uncomposed is a different skip (there was no prose to check at
        # all) and does not demote: the deterministic checks still stand on their own.
        grade = "partial"
    else:
        grade = "verified"
    return VerificationReport(grade=grade, checks=checks)


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
    not_truncated / freshness) are structural and never count either way.
    Every skip's reason stays visible on the checks array the caller receives."""
    semantic = [c for c in checks if c.name in ("definition_applied", "intent_alignment")]
    # A list with no semantic checks at all is a synthetic/partial one (tests,
    # folds) — build_report always carries both, so only a real report where
    # they ALL abstained loses the badge.
    return bool(semantic) and not any(c.status == "pass" for c in semantic)
