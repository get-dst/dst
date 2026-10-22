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
exact comparison only (the repair model handles case), and a dictionary is
the one measured on the table the SQL reads — a column name declared on more
than one entity contributes nothing unless its table resolves.

A probed value lands in LLM feedback, which is a prompt, which leaves the
building — so the probe reads only columns the lens itself exposes, and only
from the table the SQL already named.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from services.contracts.profile import LOW_CARDINALITY_MAX, TableProfile
from services.contracts.protocols import Connector
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.bounded import WarehouseTimeout

# At most this many columns are probed for one empty result — the cost bound.
EMPTY_PROBE_MAX_COLUMNS = 2


@dataclass(frozen=True)
class UnknownLiteral:
    column: str
    literal: str
    values: tuple[str, ...]  # the complete domain the literal missed


_MONTH_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def period_fields(model: SemanticModel, profiles: list[TableProfile]) -> dict[str, str]:
    """entity name -> the string field that IS a month period ("YYYY-MM"),
    read off the column profile's sample: every profiled value matches the
    form. Unlike a value dictionary the sample need not be complete — a
    period column's FORM is proven by any sample of it, and the window
    compiles to a range, never to a choice over the values."""
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, str] = {}
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        for field in entity.fields:
            if field.type != "string":
                continue
            cp = columns.get(field.name)
            values = list(cp.top_values or []) if cp is not None else []
            if values and all(_MONTH_PERIOD.match(str(v)) for v in values):
                out[entity.name] = field.name
                break
    return out


def entity_domains(
    model: SemanticModel, profiles: list[TableProfile]
) -> dict[str, dict[str, list[str]]]:
    """entity name -> column name (lowercased) -> its COMPLETE value dictionary.

    The typed resolver decides one entity at a time, so it reads the entity's
    OWN dictionary: a `month` or `status` column that means different values
    on different tables is a domain here, where ``value_domains`` (keyed by
    bare column name, for SQL the resolver did not write) must drop it."""
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, dict[str, list[str]]] = {}
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        own: dict[str, list[str]] = {}
        for field in entity.fields:
            cp = columns.get(field.name)
            if cp is None or not cp.top_values or not cp.values_complete:
                continue
            own[field.name.lower()] = list(cp.top_values)
        if own:
            out[entity.name] = own
    return out


def entity_unsampled(model: SemanticModel, profiles: list[TableProfile]) -> dict[str, str]:
    """entity name -> why its table's values were never sampled (over the
    sampling budget), for every entity whose stored profile is catalog-only
    for that reason. No dictionary exists for such an entity's columns, and
    the places that ask for a value say why."""
    reasons = {p.table: p.unsampled for p in profiles if p.unsampled}
    return {
        e.name: f"its table {e.source.table} is {reasons[e.source.table]}"
        for e in model.entities
        if e.source.table in reasons
    }


def entity_column_types(
    model: SemanticModel, profiles: list[TableProfile]
) -> dict[str, dict[str, str]]:
    """entity name -> column name (lowercased) -> the warehouse's own type for
    it, as the stored profile recorded it. The compiler casts a window bound
    to it where the declared type cannot say which of a warehouse's moment
    types the column is, and binds a quoted value to it (``bind_physical``).
    Every profiled column of the entity's table counts, declared field or
    not: a population or metric filter may read a column no field names."""
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, dict[str, str]] = {}
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        own = {name.lower(): c.type for name, c in columns.items() if c.type}
        if own:
            out[entity.name] = own
    return out


def value_domains(model: SemanticModel, profiles: list[TableProfile]) -> dict[str, list[str]]:
    """column name (lowercased) -> its COMPLETE value dictionary, for SQL whose
    table the caller cannot resolve.

    Only columns that are FIELDS on a modeled entity (generated SQL can only
    reference those) and only ``values_complete`` dictionaries. A bare name is
    kept only when ONE entity declares the field at all: a `status` complete on
    one table and merely sampled on another would otherwise refuse the second
    table's literals against the first table's dictionary — a false
    clarification, which this guard forbids. ``table_domains`` keeps every
    dictionary under its own table for a check that can resolve the table.
    """
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, list[str]] = {}
    declared_on: dict[str, int] = {}
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        for field in entity.fields:
            name = field.name.lower()
            declared_on[name] = declared_on.get(name, 0) + 1
            cp = columns.get(field.name)
            if cp is None or not cp.top_values or not cp.values_complete:
                continue
            out.setdefault(name, list(cp.top_values))
    return {name: values for name, values in out.items() if declared_on[name] == 1}


def table_domains(
    model: SemanticModel, profiles: list[TableProfile]
) -> dict[str, dict[str, list[str]]]:
    """source table (lowercased) -> column name (lowercased) -> its COMPLETE value
    dictionary: the same dictionaries as ``value_domains``, keyed by the table
    they were measured on, so a literal is checked against the dictionary of
    the table the SQL reads and never against a namesake's."""
    by_table = {p.table: {c.name: c for c in p.columns} for p in profiles}
    out: dict[str, dict[str, list[str]]] = {}
    for entity in model.entities:
        columns = by_table.get(entity.source.table, {})
        for field in entity.fields:
            cp = columns.get(field.name)
            if cp is None or not cp.top_values or not cp.values_complete:
                continue
            out.setdefault(entity.source.table.lower(), {})[field.name.lower()] = list(
                cp.top_values
            )
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


def _table_map(stmt: exp.Expr) -> dict[str, str]:
    """alias or bare table name (lowercased) -> the table as the SQL spells it."""
    tables: dict[str, str] = {}
    for tbl in stmt.find_all(exp.Table):
        full = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
        tables[tbl.name.lower()] = full
        if tbl.alias:
            tables[tbl.alias.lower()] = full
    return tables


def _table_domain(
    table: str, column: str, table_domains: dict[str, dict[str, list[str]]]
) -> list[str] | None:
    """The dictionary of *column* on *table*, matching the modeled source
    table by its spelling or by its trailing qualifiers (SQL may omit a
    leading catalog or schema the source declares, never add one)."""
    table = table.lower()
    for source, columns in table_domains.items():
        if source == table or source.endswith("." + table):
            return columns.get(column)
    return None


def unknown_literals(
    sql: str,
    dialect: str,
    domains: dict[str, list[str]],
    table_domains: dict[str, dict[str, list[str]]] | None = None,
) -> list[UnknownLiteral]:
    """Every ``=``/``IN`` string literal that misses its column's complete domain.

    With ``table_domains`` the column's dictionary is the one measured on the
    table the SQL reads for that column: a qualified column through its alias,
    a bare column through the statement's sole table. A bare column in a
    multi-table statement cannot be resolved and contributes nothing — a
    namesake's dictionary is never consulted. ``domains`` (bare names, each
    declared by one entity only) serves SQL with no table to resolve.
    """
    out: list[UnknownLiteral] = []
    seen: set[tuple[str, str]] = set()
    tables: dict[str, str] | None = None
    sole_table: str | None = None
    if table_domains:
        try:
            tables = _table_map(sqlglot.parse_one(sql, read=dialect))
        except Exception:  # noqa: BLE001 — unparseable SQL is not this guard's business
            return []
        sole_table = next(iter(tables.values())) if len(set(tables.values())) == 1 else None
    for column, literals in _string_filters(sql, dialect):
        name = column.name.lower()
        if tables is not None and table_domains:
            table = tables.get(column.table.lower()) if column.table else sole_table
            domain = _table_domain(table, name, table_domains) if table else None
        else:
            domain = domains.get(name)
        if domain is None:
            continue
        for literal in literals:
            key = (name, literal)
            if literal not in domain and key not in seen:
                seen.add(key)
                out.append(
                    UnknownLiteral(column=column.name, literal=literal, values=tuple(domain))
                )
    return out


def shown_values(values: tuple[str, ...], near: tuple[str, ...]) -> list[str]:
    """The values quoted back at a model or a caller for a missed literal: the
    whole dictionary while it is enum-sized, else the LOW_CARDINALITY_MAX
    stored values closest to the literal(s) — a 500-name dictionary is a fact
    to check against, never a list to paste into a prompt."""
    if len(values) <= LOW_CARDINALITY_MAX:
        return list(values)
    by_lower = {v.lower(): v for v in values}
    close: list[str] = []
    for lit in near:
        for hit in difflib.get_close_matches(lit.lower(), list(by_lower), n=LOW_CARDINALITY_MAX):
            if by_lower[hit] not in close:
                close.append(by_lower[hit])
    return close[:LOW_CARDINALITY_MAX]


def _describe_values(values: tuple[str, ...], near: tuple[str, ...]) -> str:
    shown = ", ".join(f"'{v}'" for v in shown_values(values, near))
    if len(values) <= LOW_CARDINALITY_MAX:
        return f"its complete value set is {shown}"
    if not shown:
        return f"it holds {len(values)} values, none close to it"
    return f"it holds {len(values)} values; the closest stored ones are {shown}"


def repair_feedback(misses: list[UnknownLiteral], sql: str) -> str:
    """The repair-loop message: name the miss and the real values, verbatim."""
    lines = [
        f"the filter value '{m.literal}' does not exist in `{m.column}` — "
        + _describe_values(m.values, (m.literal,))
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
    tables = _table_map(stmt)
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
        except WarehouseTimeout:
            raise  # a warehouse that is not answering ends the request, not this probe
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
    shown = shown_values(values, missing)
    stored = ", ".join(f"'{v}'" for v in shown)
    held = (
        f"its stored values are {stored}"
        if len(values) <= LOW_CARDINALITY_MAX
        else f"it holds {len(values)} values" + (f", the closest being {stored}" if shown else "")
    )
    return ClarificationRequest(
        term=column,
        question=(
            f"{missed} does not appear in `{column}` — {held}. "
            "Re-ask with the value you mean, or read the absence as the answer."
        ),
        options=shown,
        kind="unknown_value",
    )


def unconfirmed_zero_text(sql: str, dialect: str | None = None) -> str:
    """The ONE narration for an empty result whose emptiness could not be
    confirmed. Written by code, not the composer: left to a model, the same
    event coin-flips between "zero customers use the booking widget" (a
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
