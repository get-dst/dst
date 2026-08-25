"""value_guard — the pipeline's investigation reflex for suspicious filter literals.

The failure it exists for: asked for sales in a specific country, generation wrote
``WHERE country = 'Finland'`` against a column holding ``'FI'``, zero rows came
back, and the answer served as a confident "no sales in Finland". Nothing
errored, so nothing repaired — a filter written from the question's vocabulary
instead of the column's is invisible to every structural guard.

Two checks, both generation-path only and both feeding the existing repair loop:

- **Known domain (pre-execution, free).** When the probe artifact materialized a
  COMPLETE value dictionary for a column, an ``=``/``IN`` string literal outside
  it can only match rows by accident. `unknown_literals` finds those before the
  warehouse is touched; the feedback names the real values.
- **No domain (post-execution, one governed probe).** A zero-EVIDENCE result —
  zero rows, or `zero_evidence`'s one-row aggregate costume (COUNT → 0, other
  aggregates → NULL) — with string-literal filters is the confident-zero shape.
  `empty_result_probe` runs ≤2 ``SELECT DISTINCT col`` reads (read-only,
  row-capped) on the filtered columns; a literal proven absent from an
  enumerable column becomes the same repair feedback.

When repairs are exhausted and the literal is still PROVEN absent (by domain or
by probe), the pipeline does not serve the zero — it returns an
``unknown_value`` clarification naming the stored values: the ask-don't-guess
doctrine extended from governed terms to warehouse values. A literal that IS
present means the zero is honest and serves unchanged; a probe that taught
nothing serves the zero too, but graded below `verified` — nobody checked it.

Bias, by filter_guard's explicit contract: UNDER-detection is acceptable, FALSE
POSITIVES ARE NOT. Hence string literals only (a numeric miss is rarely a
vocabulary problem), complete dictionaries only (a partial one proves nothing),
exact comparison only (the repair model handles case), and a column name whose
domains conflict across entities contributes nothing.

A probed value lands in LLM feedback, which is a prompt, which leaves the
building — so the probe reads only columns the lens itself exposes, and only
from the table the SQL already named.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from services.contracts.profile import LOW_CARDINALITY_MAX, TableProfile
from services.contracts.protocols import Connector
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import QueryResult

# At most this many columns are probed for one empty result — the cost bound.
EMPTY_PROBE_MAX_COLUMNS = 2


@dataclass(frozen=True)
class UnknownLiteral:
    column: str
    literal: str
    values: tuple[str, ...]  # the complete domain the literal missed


def value_domains(model: SemanticModel, profiles: list[TableProfile]) -> dict[str, list[str]]:
    """column name (lowercased) -> its COMPLETE value dictionary.

    Only columns that are FIELDS on a modeled entity (generated SQL can only
    reference those) and only ``values_complete`` dictionaries. A name mapped to
    two DIFFERENT dictionaries across entities is dropped: qualifying which
    table's domain applies would need the SQL, and a wrong domain in feedback is
    worse than none.
    """
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, list[str]] = {}
    conflicted: set[str] = set()
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        for field in entity.fields:
            name = field.name.lower()
            cp = columns.get(field.name)
            if cp is None or not cp.top_values or not cp.values_complete:
                continue
            if name in out and out[name] != cp.top_values:
                conflicted.add(name)
                continue
            out[name] = list(cp.top_values)
    for name in conflicted:
        out.pop(name, None)
    return out


def _string_filters(sql: str, dialect: str) -> list[tuple[exp.Column, list[str]]]:
    """(column node, its ``=``/``IN`` string literals) per comparison in *sql*.

    Returns [] on any parse failure — this guard never blocks a query the
    structural guards accepted.
    """
    try:
        stmt = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is not this guard's business
        return []
    out: list[tuple[exp.Column, list[str]]] = []
    for eq in stmt.find_all(exp.EQ):
        column, literal = eq.this, eq.expression
        if isinstance(literal, exp.Column) and isinstance(column, exp.Literal):
            column, literal = literal, column  # 'FI' = country
        if (
            isinstance(column, exp.Column)
            and isinstance(literal, exp.Literal)
            and literal.is_string
        ):
            out.append((column, [str(literal.this)]))
    for isin in stmt.find_all(exp.In):
        column = isin.this
        literals = [
            str(e.this) for e in isin.expressions if isinstance(e, exp.Literal) and e.is_string
        ]
        if isinstance(column, exp.Column) and literals:
            out.append((column, literals))
    return out


def unknown_literals(sql: str, dialect: str, domains: dict[str, list[str]]) -> list[UnknownLiteral]:
    """Every ``=``/``IN`` string literal that misses its column's complete domain."""
    out: list[UnknownLiteral] = []
    seen: set[tuple[str, str]] = set()
    for column, literals in _string_filters(sql, dialect):
        domain = domains.get(column.name.lower())
        if domain is None:
            continue
        for literal in literals:
            key = (column.name.lower(), literal)
            if literal not in domain and key not in seen:
                seen.add(key)
                out.append(
                    UnknownLiteral(column=column.name, literal=literal, values=tuple(domain))
                )
    return out


def repair_feedback(misses: list[UnknownLiteral], sql: str) -> str:
    """The repair-loop message: name the miss and the real values, verbatim."""
    lines = [
        f"the filter value '{m.literal}' does not exist in `{m.column}` — its complete "
        "value set is " + ", ".join(f"'{v}'" for v in m.values)
        for m in misses
    ]
    return (
        "The previous SQL filters on values the warehouse does not hold: "
        + "; ".join(lines)
        + ". Rewrite the filter using the stored values that mean what the question "
        f"asks (or answer from what exists). Previous SQL: {sql}"
    )


@dataclass(frozen=True)
class ProbeFinding:
    """One filtered column whose literal(s) the probe proved absent."""

    column: str
    values: tuple[str, ...]  # what the column actually holds (enumerable)
    missing: tuple[str, ...]  # the filter literals absent from it


@dataclass(frozen=True)
class ProbeOutcome:
    """What one empty-result investigation established.

    ``findings`` are actionable (repair or clarify). ``confirmed`` means every
    probed column enumerated AND held its literals — the zero is the data's own
    answer. ``attempted`` with neither means the probes taught nothing
    (unenumerable columns, probe errors): the zero serves, but ungraded as
    verified — nobody checked it.
    """

    findings: tuple[ProbeFinding, ...] = ()
    confirmed: bool = False
    attempted: bool = False


def zero_evidence(sql: str, dialect: str, result: QueryResult) -> bool:
    """True when a ONE-ROW aggregate result proves the WHERE matched nothing.

    COUNT over an empty set is 0 and every other aggregate is NULL, so an
    ungrouped, all-aggregate SELECT returning exactly (COUNT → 0, rest → NULL)
    is the zero-rows observation wearing a one-row costume — the confident zero
    that `has_rows` cannot see. Bare aggregates only (``ROUND(AVG(x))`` bails):
    under-detection over a false positive, always.
    """
    if len(result.rows) != 1:
        return False
    try:
        stmt = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is not this guard's business
        return False
    if not isinstance(stmt, exp.Select) or stmt.args.get("group") is not None:
        return False
    projections = stmt.expressions
    if not projections or len(projections) != len(result.rows[0]):
        return False
    for node, cell in zip(projections, result.rows[0], strict=True):
        inner = node.this if isinstance(node, exp.Alias) else node
        if isinstance(inner, exp.Count):
            if cell is None or cell != 0:
                return False
        elif isinstance(inner, exp.Sum | exp.Avg | exp.Min | exp.Max):
            if cell is not None:
                return False
        else:
            return False
    return True


def empty_result_probe(sql: str, model: SemanticModel, connector: Connector) -> ProbeOutcome:
    """Investigate a zero-evidence result: does its filtered column even hold the literal?

    For ≤``EMPTY_PROBE_MAX_COLUMNS`` string-filtered columns, one
    governed ``SELECT DISTINCT`` (read-only, capped at LOW_CARDINALITY_MAX + 1
    rows) against the column's own table, resolved from the SQL's alias map.
    A literal proven absent from an enumerable column is a `ProbeFinding`; every
    probed column enumerating AND holding its literals is ``confirmed``; probes
    that taught nothing leave only ``attempted`` — the caller grades that zero
    honestly instead of trusting it.
    """
    dialect = model.dialect
    try:
        stmt = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is not this guard's business
        return ProbeOutcome()
    tables: dict[str, str] = {}
    for tbl in stmt.find_all(exp.Table):
        full = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
        tables[tbl.name.lower()] = full
        if tbl.alias:
            tables[tbl.alias.lower()] = full
    sole_table = next(iter(tables.values())) if len(set(tables.values())) == 1 else None

    probed = 0
    resolved_present = 0
    unresolved = 0
    findings: list[ProbeFinding] = []
    seen: set[str] = set()
    for column, literals in _string_filters(sql, dialect):
        name = column.name
        if name.lower() in seen or probed >= EMPTY_PROBE_MAX_COLUMNS:
            continue
        table = tables.get(column.table.lower()) if column.table else sole_table
        if table is None:
            continue
        seen.add(name.lower())
        probe_sql = (
            exp.select(exp.column(name))
            .distinct()
            .from_(exp.to_table(table))
            .limit(LOW_CARDINALITY_MAX + 1)
            .sql(dialect=dialect)
        )
        probed += 1
        try:
            result = connector.execute(probe_sql, read_only=True, row_limit=LOW_CARDINALITY_MAX + 1)
        except Exception:  # noqa: BLE001 — a failed probe proves nothing
            unresolved += 1
            continue
        values = [str(row[0]) for row in result.rows if row and row[0] is not None]
        if not values or len(values) > LOW_CARDINALITY_MAX:
            unresolved += 1  # unenumerable — nothing concrete learned
            continue
        missing = tuple(lit for lit in literals if lit not in values)
        if missing:
            findings.append(
                ProbeFinding(column=name, values=tuple(sorted(values)), missing=missing)
            )
        else:
            resolved_present += 1
    return ProbeOutcome(
        findings=tuple(findings),
        confirmed=not findings and resolved_present > 0 and unresolved == 0,
        attempted=probed > 0,
    )


def probe_feedback(findings: tuple[ProbeFinding, ...], sql: str) -> str:
    """The repair-loop message a probe's findings become."""
    lines = [
        f"`{f.column}` actually holds: "
        + ", ".join(f"'{v}'" for v in f.values)
        + " — the filter value(s) "
        + ", ".join(f"'{m}'" for m in f.missing)
        + " match none of them"
        for f in findings
    ]
    return (
        "The query returned zero rows, and a probe of the filtered column(s) shows why: "
        + "; ".join(lines)
        + ". Rewrite the filter using the stored values that mean what the question asks "
        f"(or answer from what exists). Previous SQL: {sql}"
    )


def unknown_value_clarification(
    column: str, missing: tuple[str, ...], values: tuple[str, ...]
) -> ClarificationRequest:
    """Repairs exhausted with the literal PROVEN absent: ask, don't serve the zero.

    The ask-don't-guess doctrine extended from governed terms to warehouse
    values — `term` names the COLUMN, `options` are what it actually holds. A
    caller who meant a stored value re-asks with it; a caller who meant the
    absent one now knows the absence IS the answer, stated instead of implied
    by an unexplained 0.
    """
    missed = ", ".join(f"'{m}'" for m in missing)
    stored = ", ".join(f"'{v}'" for v in values)
    return ClarificationRequest(
        term=column,
        question=(
            f"{missed} does not appear in `{column}` — its stored values are {stored}. "
            "Re-ask with the value you mean, or read the absence as the answer."
        ),
        options=list(values),
        kind="unknown_value",
    )


def unconfirmed_zero_text(sql: str, dialect: str | None = None) -> str:
    """The ONE narration for an empty result whose emptiness could not be
    confirmed. Written by code, not the composer: left to a model, the same
    event coin-flips between "zero customers are using AI replies" (a
    board-level wrong answer asserted as fact) and an honest "I cannot provide
    a count". A detected-unsafe zero must never be a business finding, so no
    model writes this sentence.

    Names the literal date filters when the SQL carries any — a hardcoded
    'today' against a lagging snapshot is the usual cause."""
    dates: list[str] = []
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        for eq in tree.find_all(exp.EQ):
            lit = eq.expression
            if isinstance(lit, exp.Cast):
                lit = lit.this
            if isinstance(lit, exp.Literal) and lit.is_string:
                col = eq.this
                if isinstance(col, exp.Column):
                    dates.append(f"`{col.name}` = '{lit.this}'")
    except Exception:  # noqa: BLE001 — narration must never crash the serve
        pass
    cause = (
        " The result was filtered to " + " and ".join(dates[:3]) + " — if that date has no "
        "data yet (snapshots lag), the zero is a filter artifact, not a business fact."
        if dates
        else " The most likely cause is a filter that matched no rows."
    )
    return (
        "The query returned no matching rows, and the filtered values could not be "
        "confirmed to exist — so I cannot confirm this as a real zero, and I am not "
        "reporting it as one." + cause + " Re-ask naming the latest available date, "
        "or check the filters shown in the SQL."
    )
