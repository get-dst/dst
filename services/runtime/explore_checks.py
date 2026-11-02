"""The checks an outside-lens answer carries beyond its grain line.

An answer read from a table the lens exposes without definitions (the `sql`
tool, the explore route) has no governed meaning to lean on, so dst verifies
what it can measure and says the rest plainly. Beside rows against distinct
key (``runtime.grain``) and where the data ends (``api.sql``), three more
checks run before the number is served:

  * ``join`` — row conservation: the rows the statement's joins produce
    against the rows of its base table. A join that multiplies rows inflates
    every count and sum over the base table's columns.
  * ``clock`` — every time column the statement reads is named with what it
    records, from the table's blueprint; one with no declared clock is said
    to be unexplained rather than left to the reader's guess.
  * ``reconcile`` — when a certified answer of the lens reads the same tables
    over the same window, its number is computed beside the statement's and
    the delta disclosed.

Each check is one line on the receipt (``name:status (mode): detail``), and a
check that could not run says ``could not close`` and why. None of them
raises the grade: an outside-lens answer is capped as before; these say what
the cap covers.
"""

from __future__ import annotations

import decimal
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp

from services.contracts.failure_modes import FailureMode
from services.contracts.protocols import Connector
from services.contracts.semantic_model import Entity, SemanticModel
from services.contracts.verification import VerificationCheck, VerificationReport
from services.runtime import grain

ExploreStatus = Literal["pass", "disclose", "skip"]

# The sentence a check that could not run begins with: said the same way on
# every surface, so a reader can grep a log for it.
COULD_NOT_CLOSE = "could not close"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ExploreCheck:
    """One check an outside-lens answer carries, with the mode a reader
    counts it under when it did not pass."""

    name: str
    status: ExploreStatus
    detail: str
    mode: FailureMode | None = None

    @property
    def line(self) -> str:
        """The receipt's spelling: ``join:disclose (wrong_grain): …``."""
        tag = f" ({self.mode})" if self.mode is not None else ""
        return f"{self.name}:{self.status}{tag}: {self.detail}"


def _could_not_close(name: str, why: str) -> ExploreCheck:
    return ExploreCheck(
        name=name,
        status="skip",
        detail=f"{COULD_NOT_CLOSE}: {why}",
        mode=FailureMode.unverifiable,
    )


def _parsed(sql: str, dialect: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read=dialect)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — unparseable SQL closes no check
        return None


# ── join: row conservation ───────────────────────────────────────────────────


def _outer_select(stmt: exp.Expression) -> exp.Select | None:
    """The statement's outermost SELECT: the one whose FROM and JOINs decide
    how many rows reach the aggregate. A set operation has no single base."""
    return stmt if isinstance(stmt, exp.Select) else None


def join_check(
    sql: str, model: SemanticModel, connector: Connector | None, *, connection: str
) -> ExploreCheck | None:
    """Rows after the statement's joins against the rows of its base table,
    both counted live within the sampling budget. None when the statement
    joins nothing: there is no conservation to check."""
    stmt = _parsed(sql, model.dialect)
    if stmt is None:
        return None
    select = _outer_select(stmt)
    if select is None or not select.args.get("joins"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")  # key renamed across sqlglot
    sources = [from_.this if from_ is not None else None] + [
        j.this for j in select.args.get("joins") or []
    ]
    # A CTE reference parses as a table; counting it without its WITH is not
    # a count of anything, so it is a subquery here (a qualified name is never
    # a CTE).
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}

    def physical(source: object) -> bool:
        return isinstance(source, exp.Table) and (
            bool(source.db or source.catalog) or source.name.lower() not in ctes
        )

    if not all(physical(s) for s in sources):
        return _could_not_close(
            "join", "the statement joins a subquery or a CTE, whose rows are not a table's"
        )
    assert from_ is not None and isinstance(from_.this, exp.Table)
    base_table = from_.this
    base_name = ".".join(p for p in (base_table.catalog, base_table.db, base_table.name) if p)
    if connector is None:
        return _could_not_close("join", "no live connection to count the join with")
    count = exp.Count(this=exp.Star())
    joined = exp.select(count).from_(from_.this.copy())
    for j in select.args.get("joins") or []:
        joined = joined.join(j.copy())
    base = exp.select(count).from_(base_table.copy())
    probe = (
        exp.select(
            exp.alias_(exp.paren(joined), "joined", quoted=False),
            exp.alias_(exp.paren(base), "base", quoted=False),
        )
    ).sql(dialect=model.dialect)
    row, skipped = grain.bounded_row(
        connector, probe, step="the join's row count", connection=connection
    )
    if row is None:
        return _could_not_close("join", skipped or "the join's row count did not run")
    try:
        after, before = int(str(row[0])), int(str(row[1]))
    except (IndexError, TypeError, ValueError):
        return _could_not_close("join", "the join's row count returned no counts")
    others = ", ".join(
        ".".join(p for p in (s.catalog, s.db, s.name) if p)
        for s in sources[1:]
        if isinstance(s, exp.Table)
    )
    if after > before:
        return ExploreCheck(
            name="join",
            status="disclose",
            detail=f"{base_name} joined to {others} gives {after:,} rows from {before:,} rows "
            f"of {base_name}: the join multiplies rows, so a count or sum over {base_name}'s "
            "columns is inflated unless it reads distinct keys",
            mode=FailureMode.wrong_grain,
        )
    kept = (
        f"keeps one row per row of {base_name} ({after:,})"
        if after == before
        else f"keeps {after:,} of {before:,} rows of {base_name} (the join drops the rest)"
    )
    return ExploreCheck(name="join", status="pass", detail=f"{base_name} joined to {others} {kept}")


# ── clock: every time column read, named ─────────────────────────────────────


def clock_columns(entity: Entity, model: SemanticModel) -> list[str]:
    """The dated columns of an exposed table: its blueprint's clocks, else
    every date or timestamp field the lens reads."""
    blueprint = model.blueprints.get(entity.source.table)
    if blueprint is not None and blueprint.clocks:
        return list(blueprint.clocks)
    dated = ("date", "timestamp", "time", "datetime")
    return [f.name for f in entity.fields if (f.type or "").lower() in dated]


def _columns_read(sql: str, dialect: str) -> set[str]:
    stmt = _parsed(sql, dialect)
    if stmt is None:
        return set()
    return {c.name.lower() for c in stmt.find_all(exp.Column) if c.name}


def clock_checks(sql: str, model: SemanticModel, tables: Sequence[str]) -> list[ExploreCheck]:
    """One check per time column the statement reads from an exposed table:
    what the column records per its blueprint, or that no clock is declared."""
    read = _columns_read(sql, model.dialect)
    out: list[ExploreCheck] = []
    for table in tables:
        entity = next((e for e in model.entities if e.source.table == table), None)
        if entity is None:
            continue
        blueprint = model.blueprints.get(table)
        clocks = {k.lower(): v for k, v in (blueprint.clocks if blueprint else {}).items()}
        for column in clock_columns(entity, model):
            if column.lower() not in read:
                continue
            clock = clocks.get(column.lower())
            if clock is None or not clock.meaning:
                out.append(
                    _could_not_close(
                        "clock",
                        f"{table}.{column} is read as a time column and no clock is declared "
                        "for it (semantic/tables/), so what its dates record is not known",
                    )
                )
                continue
            zone = f", stored in {clock.timezone}" if clock.timezone else ""
            out.append(
                ExploreCheck(
                    name="clock",
                    status="pass",
                    detail=f"{table}.{column} records {clock.meaning}{zone}",
                )
            )
    return out


# ── reconcile: against a governed number over the same tables and window ─────


def _number(row: Sequence[object] | None) -> float | None:
    """The first numeric cell of a row: the figure an answer leads with."""
    if row is None:
        return None
    for cell in row:
        if isinstance(cell, bool):
            continue
        if isinstance(cell, int | float | decimal.Decimal):
            value = float(cell)
            return value if math.isfinite(value) else None
    return None


def _window(sql: str) -> tuple[str, ...]:
    return tuple(sorted(set(_DATE.findall(sql))))


def _tables(sql: str, dialect: str) -> frozenset[str]:
    from services.certify.bindings import source_tables

    try:
        return frozenset(source_tables(sql, dialect))
    except Exception:  # noqa: BLE001 — unparseable SQL reads nothing to reconcile against
        return frozenset()


def reconcile_check(
    sql: str,
    rows: Sequence[Sequence[object]],
    candidates: Sequence[tuple[str, str]],
    model: SemanticModel,
    connector: Connector | None,
    *,
    connection: str,
) -> ExploreCheck:
    """The statement's leading number against a certified answer of the lens
    that reads the same tables over the same window (the date literals both
    carry), computed live. ``candidates`` are ``(question, sql)`` pairs; the
    first that qualifies is the governed number."""
    mine = _number(rows[0] if rows else None)
    if mine is None:
        return _could_not_close("reconcile", "the statement returns no number to reconcile")
    tables, window = _tables(sql, model.dialect), _window(sql)
    if not tables:
        return _could_not_close("reconcile", "the statement's tables could not be read")
    match = next(
        (
            (question, approved)
            for question, approved in candidates
            if approved.strip()
            and _tables(approved, model.dialect) == tables
            and _window(approved) == window
        ),
        None,
    )
    named = ", ".join(sorted(tables))
    if match is None:
        when = f"over {' to '.join(window)}" if window else "with no window"
        return _could_not_close(
            "reconcile", f"no certified answer of this lens reads {named} {when}"
        )
    question, approved = match
    if connector is None:
        return _could_not_close("reconcile", "no live connection to compute the certified number")
    row, skipped = grain.bounded_row(
        connector, approved, step="the certified answer", connection=connection
    )
    theirs = _number(row)
    if theirs is None:
        return _could_not_close(
            "reconcile",
            skipped or f"the certified answer to '{question}' returned no number",
        )
    if math.isclose(mine, theirs, rel_tol=1e-9, abs_tol=1e-9):
        return ExploreCheck(
            name="reconcile",
            status="pass",
            detail=f"matches the certified answer to '{question}' over {named}: {_fmt(theirs)}",
        )
    delta = mine - theirs
    share = f" ({delta / theirs:+.1%})" if theirs else ""
    return ExploreCheck(
        name="reconcile",
        status="disclose",
        detail=f"differs from the certified answer to '{question}' over {named}: this "
        f"statement {_fmt(mine)}, certified {_fmt(theirs)}, delta {_fmt(delta, signed=True)}"
        f"{share}",
        mode=FailureMode.unverifiable,
    )


def _fmt(value: float, *, signed: bool = False) -> str:
    sign = "+" if signed else ""
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):{sign},}"
    return f"{value:{sign},.4f}".rstrip("0").rstrip(".")


# ── folding ──────────────────────────────────────────────────────────────────


def fold(report: VerificationReport, checks: Sequence[ExploreCheck]) -> VerificationReport:
    """The checks on an answer's report: a disclosure fails its check and
    keeps the grade short of verified; a check that could not close skips.
    Never upgrades a grade."""
    if not checks:
        return report
    from services.runtime.verification import partial_reason

    out = list(report.checks)
    grade = report.grade
    for c in checks:
        status: Literal["pass", "fail", "skip"] = (
            "pass" if c.status == "pass" else "fail" if c.status == "disclose" else "skip"
        )
        out.append(VerificationCheck(name=c.name, status=status, reason=c.line))
        if c.status != "pass" and grade == "verified":
            grade = "partial"
    return VerificationReport(grade=grade, checks=out, partial_reason=partial_reason(grade, out))


def lines(checks: Sequence[ExploreCheck]) -> list[str]:
    """The `degraded` lines an answer carries, in the EXPOSED/GRAIN idiom."""
    return [f"{c.name.upper()}: {c.detail}" for c in checks]


def mode_of(checks: Sequence[ExploreCheck]) -> FailureMode | None:
    """The mode the receipt carries: the first disclosure's, else None (a
    check that could not close names its mode on its own line)."""
    return next((c.mode for c in checks if c.status == "disclose"), None)
