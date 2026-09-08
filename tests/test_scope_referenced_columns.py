"""Scope follows what the model REFERENCES, not only `fields:`.

The guard's allow-list was built from ``fields:`` alone, so three authoring
shapes compiled clean and then failed (or silently misbehaved) at query time:

* a **bare passthrough dimension** — ``dimensions: [{name: status}]``, which the
  compiler resolves to the field of the same name — rejected every query that
  touched it as "column status is out of lens scope";
* a column named only inside a **metric expr**, same rejection;
* a column named only by **population_filter** — worse than a rejection: on a
  scalar question the predicate was dropped and an undeduped number served
  (€4,014,025.07 where the deduped book was €3,846,737.37).

Two engineers hit the dimension case four times across twelve entities and
called it "the single most common way to break a new entity in this tool"; the
workaround was to redeclare every referenced column under ``fields:``.

A dimension or population filter the curator wrote IS the curator modelling
that column. What the guard must still refuse is a column nobody modelled.
"""

from __future__ import annotations

from services.contracts.semantic_model import SemanticModel, _referenced_columns


def _model(entity: dict[str, object]) -> SemanticModel:
    return SemanticModel.model_validate({"lens": "t", "dialect": "duckdb", "entities": [entity]})


BASE: dict[str, object] = {
    "name": "orders",
    "source": {"connection": "wh", "table": "orders"},
    "fields": [{"name": "order_id", "type": "integer"}],
}


def test_bare_dimension_is_in_scope() -> None:
    m = _model({**BASE, "dimensions": [{"name": "status"}]})
    assert "status" in m.allowed_columns()["orders"]


def test_dimension_expr_columns_are_in_scope() -> None:
    m = _model({**BASE, "dimensions": [{"name": "band", "expr": "orders.amount_eur"}]})
    cols = m.allowed_columns()["orders"]
    assert "amount_eur" in cols
    assert "band" in cols  # the dimension's own name still counts


def test_metric_expr_columns_are_in_scope() -> None:
    m = _model(
        {**BASE, "metrics": [{"name": "revenue", "agg": "sum", "expr": "orders.amount_eur"}]}
    )
    assert "amount_eur" in m.allowed_columns()["orders"]


def test_population_filter_columns_are_in_scope() -> None:
    """The silent one: an undeclared column here dropped the WHERE clause."""
    m = _model({**BASE, "population_filter": "NOT _fivetran_deleted"})
    assert "_fivetran_deleted" in m.allowed_columns()["orders"]


def test_time_field_and_primary_key_are_in_scope() -> None:
    m = _model({**BASE, "default_time_field": "order_date", "primary_key": ["order_id"]})
    cols = m.allowed_columns()["orders"]
    assert {"order_date", "order_id"} <= cols


def test_unmodelled_columns_stay_out_of_scope() -> None:
    """The guard still has a job: nothing grants a column nobody named."""
    m = _model({**BASE, "dimensions": [{"name": "status"}]})
    cols = m.allowed_columns()["orders"]
    assert "salary_eur" not in cols
    assert "customer_ssn" not in cols


def test_filter_literals_never_become_columns() -> None:
    """A value like 'Revenue (SE)' must not smuggle identifiers into scope."""
    got = _referenced_columns("group_account_name IN ('Revenue (SE)', 'Elimination')", "gc")
    assert got == {"group_account_name"}


def test_sql_keywords_and_functions_are_not_columns() -> None:
    got = _referenced_columns("SUM(orders.amount) > 0 AND status IS NOT NULL", "orders")
    assert got == {"amount", "status"}
