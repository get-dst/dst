"""fanout_guard — deterministic refusal of aggregates inflated by a declared fan-out.

`Join.relationship` has existed on the contract from the start, and until now it was
spent entirely on a line of generator prompt ("Respect join relationships:
aggregating across a one_to_many join fans out"). Concretely: joining
`customer_transactions` to `customer_nodes` (a 7-row-per-customer SCD) and summing
deposits reports every number 7x too large — and every existing guard passes it.
That is the same disease as the filter and grain bugs: the prompt's rule is
advisory, so this module puts the check in code.

The rule: joining the MANY side of a declared one-to-many onto the ONE side
multiplies the one side's rows, so any additive aggregate over the one side's
columns is inflated. SUM/AVG/COUNT are additive and therefore unsafe; MIN/MAX and
COUNT(DISTINCT ...) are fan-out-invariant and never flagged.

Bias, inherited from filter_guard's explicit contract: UNDER-detection is
acceptable, FALSE POSITIVES ARE NOT. Hence we flag only when the many side is a
BARE table reference in the same select scope as the aggregate (a derived table or
CTE may already deduplicate, and proving it does not is beyond a syntactic check),
only for aggregates whose arguments are strictly attributable to the one side, and
only when the model actually declares the relationship.

VERDICT — this module does NOT reach that bar, and the reason is not a tuning
problem. Run it over a corpus of expert-written, known-correct SQL and it flags
a large minority of it: some flags are provably false (the query's own WHERE
collapses the fan-out), and even where rows genuinely duplicate, the verdict
splits both ways. One query duplicates district rows across accounts and is
CORRECT (the question's grain is accounts); another duplicates the same shape
and is wildly WRONG. Identical SQL, opposite answers — because whether a
fan-out is a bug depends on the question's intended GRAIN, and the SQL does not
carry the grain.

So a post-hoc guard is the wrong shape for this defect. The grain IS known in the
intent compiler, which is where the decision belongs and where no guard is needed
at all — compiling from a declared grain is fan-out-safe by construction. Keep
this module only as a backstop for the raw-SQL path, and only behind an explicit
warn-not-refuse setting.
"""

from __future__ import annotations

from typing import cast

import sqlglot
from sqlglot import exp

from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import SemanticModel
from services.runtime.filter_guard import _alias_map

# Additive aggregates — the ones a duplicated row changes. MIN/MAX are invariant
# under duplication, and COUNT(DISTINCT x) is handled separately below.
_UNSAFE_AGGS = (exp.Sum, exp.Avg, exp.Count)


def _tables_in_scope(scope: exp.Expression) -> set[str]:
    """Physical tables referenced as BARE table nodes directly in *scope*'s FROM/JOINs.

    Derived tables and CTE references are deliberately excluded: they may already
    deduplicate the many side, and a syntactic check cannot prove otherwise.
    """
    out: set[str] = set()
    # key renamed across sqlglot (same dance as probe/drift.py)
    frm = scope.args.get("from_") or scope.args.get("from")
    sources: list[exp.Expression] = []
    if frm is not None:
        sources.append(frm.this)
    for j in scope.args.get("joins") or []:
        sources.append(j.this)
    for src in sources:
        if isinstance(src, exp.Table) and src.name:
            out.add(".".join(p.lower() for p in (src.catalog, src.db, src.name) if p))
    return out


def _attributed_to(node: exp.Expression, table: str, alias_map: dict[str, set[str]]) -> bool:
    """True when every column under *node* is qualified and resolves to *table*.

    Strict on purpose: an unqualified column in a multi-table scope is ambiguous,
    and guessing its owner is how a guard grows false positives.
    """
    cols = list(node.find_all(exp.Column))
    if not cols:
        return False
    for col in cols:
        qualifier = col.table
        if not qualifier:
            return False
        refs = alias_map.get(qualifier.lower(), set())
        if not refs or not all(_ref_matches(r, table) for r in refs):
            return False
    return True


def inflated_aggregates(sql: str, model: SemanticModel) -> list[tuple[str, str, str]]:
    """Aggregates provably inflated by a declared one-to-many join.

    Returns ``[(aggregate_sql, one_side_entity, many_side_entity), ...]`` — empty
    when the SQL is safe, unparseable, or the model declares no fan-out join.
    """
    fanouts: list[tuple[str, str, str, str]] = []  # one_entity, one_tbl, many_entity, many_tbl
    by_name = {e.name: e for e in model.entities}
    for join in model.joins:
        left, right = by_name.get(join.left), by_name.get(join.right)
        if left is None or right is None:
            continue
        if join.relationship == "one_to_many":
            fanouts.append((left.name, left.source.table, right.name, right.source.table))
        elif join.relationship == "many_to_one":
            # Symmetric: joining left (many) onto right (one) fans out the right side.
            fanouts.append((right.name, right.source.table, left.name, left.source.table))
    if not fanouts:
        return []

    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is sql_guard's rejection, not ours
        return []
    if not isinstance(stmt, exp.Expression):
        return []

    alias_map = _alias_map(stmt)
    hits: list[tuple[str, str, str]] = []
    for scope in stmt.find_all(exp.Select):
        present = _tables_in_scope(scope)
        if len(present) < 2:
            continue
        for one_entity, one_tbl, many_entity, many_tbl in fanouts:
            if not any(_ref_matches(t, one_tbl) for t in present):
                continue
            if not any(_ref_matches(t, many_tbl) for t in present):
                continue
            for found in scope.find_all(*_UNSAFE_AGGS):
                # sqlglot 30 derives AggFunc from Expr, not Expression; the nodes ARE
                # Expressions at runtime (Sum's MRO carries both). Same cast idiom as
                # filter_guard/shape_guard use on aggregate nodes.
                agg = cast(exp.Expression, found)
                if agg.find_ancestor(exp.Select) is not scope:
                    continue  # belongs to a nested scope; that scope gets its own check
                # COUNT(DISTINCT x) — and any DISTINCT aggregate — is invariant under
                # row duplication. sqlglot models it as Count(this=Distinct(...)), NOT
                # as args["distinct"]; reading the arg silently flags every
                # COUNT(DISTINCT) there is.
                if isinstance(agg.this, exp.Distinct):
                    continue
                if not _attributed_to(agg, one_tbl, alias_map):
                    continue
                hits.append((agg.sql(dialect=model.dialect), one_entity, many_entity))
    return hits


def refusal_reason(hits: list[tuple[str, str, str]]) -> str:
    """One sentence naming the aggregate, the fan-out, and the fix."""
    agg, one, many = hits[0]
    return (
        f"`{agg}` aggregates {one} across a one-to-many join to {many}, which "
        f"duplicates every {one} row once per matching {many} row and overstates the "
        f"result. Aggregate {one} first, or join a deduplicated {many} "
        f"(SELECT DISTINCT the key and the needed attributes)."
    )
