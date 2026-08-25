"""Decompose a query's SQL into its governed scope — which table(s), which
fields/measures, which filters, how it's ranked — so a reviewer can audit an
answer without reading the SQL itself.

Parsed with sqlglot (already a dependency). Best-effort: anything it can't parse
returns None rather than raising, so a malformed query degrades to "just show the
SQL", never an error.
"""

from __future__ import annotations

import sqlglot
from pydantic import BaseModel
from sqlglot import exp


class QueryScope(BaseModel):
    """The governed decomposition of one query's SQL."""

    tables: list[str] = []  # the source table(s) — FROM / JOIN, CTE names excluded
    fields: list[str] = []  # the projected columns/expressions — the "what" measured
    filters: list[str] = []  # WHERE predicates, one per top-level AND
    order_by: list[str] = []  # how the result is ranked


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _split_and(cond: exp.Expr) -> list[exp.Expr]:
    """Flatten a top-level AND chain into individual predicates; leave OR intact."""
    if isinstance(cond, exp.And):
        return _split_and(cond.left) + _split_and(cond.right)
    if isinstance(cond, exp.Paren):
        return _split_and(cond.this)
    return [cond]


def parse_scope(sql: str | None, dialect: str | None = None) -> QueryScope | None:
    if not sql or not sql.strip():
        return None
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    if tree is None:
        return None

    # A CTE name is a local alias, not a governed table — don't list it as a source.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    tables = _dedupe([t.sql() for t in tree.find_all(exp.Table) if t.name not in cte_names])

    select = tree.find(exp.Select)
    fields = _dedupe([p.sql() for p in select.expressions]) if select else []

    where = tree.find(exp.Where)
    filters = _dedupe([p.sql() for p in _split_and(where.this)]) if where and where.this else []

    order = tree.find(exp.Order)
    order_by = _dedupe([o.sql() for o in order.expressions]) if order else []

    # sqlglot is lenient — non-SQL text parses to an empty tree rather than raising.
    # If nothing governed came out, there's no scope to show.
    if not tables and not fields:
        return None

    return QueryScope(tables=tables, fields=fields, filters=filters, order_by=order_by)
