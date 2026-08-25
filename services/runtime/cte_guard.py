"""cte_guard — deterministic binder-class check: a CTE must project what its
consumers reference.

The failure it exists for: generation writes ``WITH danish AS (SELECT acct_id,
account_name FROM crm.accounts ...)`` then joins ``danish AS a ON
a.external_ref = ...`` — a column the CTE never projected.

What this guard is NOT: the safety net that stops invalid SQL reaching the
warehouse. The dry-run gate already did that — the query never executed, and
the trace's "Binder Error: Values list \"a\" does not have a column named
\"external_ref\"" is the dry run's own words. (An earlier version of this
docstring claimed otherwise; it was wrong.)

What it IS: better repair feedback, which is the whole game when the budget is
ONE attempt. The dry run hands the loop a raw engine error, and the repair did
not land; both halves of the defect are facts of the AST — the projection list
and the reference — so a deterministic check can instead name the CTE to edit
(never the reference alias, which names nothing editable), the column to add,
and the projection list that exists. It also holds where the dry run cannot:
connectors without one, and engines whose error text says something else.

Because it fires one stage EARLIER than the dry run on the same class, it does
not add repair spend — it consumes the attempt the dry run would have consumed.

Bias, by explicit contract (the filter_guard rule): UNDER-detection is
acceptable, FALSE POSITIVES ARE NOT — and the wiring makes even a false
positive harmless, because this guard only spends repair attempts; when repairs
are exhausted the SQL flows on to the warehouse and fails (or passes) exactly
as it would have without the guard. Skipped on purpose: CTEs projecting ``*``
or anything whose output names sqlglot can't enumerate, unqualified column
references, and statements that don't parse.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


@dataclass(frozen=True)
class UnderprojectedRef:
    cte: str  # the CTE's declared name — the thing the repair must edit
    alias: str  # how the reference spells it at the violation site
    column: str
    projected: tuple[str, ...]


def _cte_output_names(source: object) -> tuple[str, tuple[str, ...]] | None:
    """(declared CTE name, output column names), or None when the outputs
    can't be known for certain (star projections, set ops sqlglot won't
    enumerate, …) — None means "never flag this CTE"."""
    expression = getattr(source, "expression", None)
    if expression is None:
        return None
    named = getattr(expression, "named_selects", None)
    if not named or any(n == "*" or not n for n in named):
        return None
    # The select inside a CTE hangs off an exp.CTE node carrying the declared
    # name; the repair message must name THAT (a reference alias like `a`
    # names nothing editable).
    parent = getattr(expression, "parent", None)
    if not isinstance(parent, exp.CTE) or not parent.alias:
        return None
    return parent.alias, tuple(n.lower() for n in named)


def underprojected_cte_refs(sql: str, dialect: str) -> list[UnderprojectedRef]:
    """Qualified column references that resolve to a CTE lacking that column
    in its projection list. Scope-aware via sqlglot (an alias that means the
    CTE in one subquery and a table in another is resolved per scope)."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        scopes = traverse_scope(tree)
    except Exception:
        return []
    misses: dict[tuple[str, str], UnderprojectedRef] = {}
    for scope in scopes:
        cte_sources = {
            name: resolved
            for name, source in scope.sources.items()
            if getattr(source, "is_cte", False)
            and (resolved := _cte_output_names(source)) is not None
        }
        if not cte_sources:
            continue
        for column in scope.expression.find_all(exp.Column):
            if not column.table or column.db or column.catalog:
                continue
            resolved = cte_sources.get(column.table) or cte_sources.get(column.table.lower())
            if resolved is None:
                continue
            cte_name, names = resolved
            if column.name.lower() not in names:
                key = (cte_name.lower(), column.name.lower())
                misses.setdefault(
                    key, UnderprojectedRef(cte_name, column.table, column.name, names)
                )
    return list(misses.values())


def repair_feedback(misses: list[UnderprojectedRef], sql: str) -> str:
    """The repair-loop message: name the CTE, the missing column, and the fix —
    sharper than the warehouse binder error that failed to land the repair."""
    lines = [
        f"the CTE `{m.cte}` is referenced as `{m.alias}.{m.column}` but does not project "
        f"`{m.column}` — its projection list is only ({', '.join(m.projected)}). "
        f"Add `{m.column}` to the SELECT list of CTE `{m.cte}` (or stop referencing it)"
        for m in misses
    ]
    return (
        "The previous SQL references CTE columns that were never projected: "
        + "; ".join(lines)
        + f". Previous SQL: {sql}"
    )
