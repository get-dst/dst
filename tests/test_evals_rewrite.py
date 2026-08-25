"""rewrite_to_sources: leaf table names repointed at live physical tables —
what apply's inbound oracle gate and `dst evals migrate` probe with."""

from __future__ import annotations

from services.evals.rewrite import rewrite_to_sources


def test_rewrite_to_sources_qualifies_and_aliases() -> None:
    out = rewrite_to_sources(
        "SELECT customers.customer_id FROM customers",
        "duckdb",
        {"customers": "prod.jaffle.customers"},
    )
    assert "FROM prod.jaffle.customers AS customers" in out
    assert "customers.customer_id" in out
