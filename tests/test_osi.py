"""OSI / Apache Ossie interchange — import, export, and what each direction loses.

The fixture is inline rather than read from the spec repo so the suite is hermetic;
it is shaped after the working group's own TPC-DS example, including the `ai_context`
OBJECT form that example actually uses (spec.yaml documents a bare string, and reading
only that form crashes on real files).
"""

from __future__ import annotations

from typing import Any

import pytest

from services.contracts.semantic_model import Definition, EntitySource, Field, Metric
from services.contracts.shared_semantic import SharedEntity, SharedJoin
from services.osi import from_osi, to_osi
from services.osi.load import read_ai_context


def _document() -> dict[str, Any]:
    def expr(sql: str) -> dict[str, Any]:
        return {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]}

    return {
        "version": "0.2.0",
        "semantic_model": [
            {
                "name": "retail",
                "ai_context": {"instructions": "Use for retail analytics."},
                "datasets": [
                    {
                        "name": "sales",
                        "source": "warehouse.public.sales",
                        "primary_key": ["sale_id"],
                        "ai_context": {
                            "instructions": "One row per sale.",
                            "synonyms": ["transactions"],
                            "examples": ["How much did we sell last month?"],
                        },
                        "fields": [
                            {
                                "name": "sale_id",
                                "expression": expr("sales.sale_id"),
                                "datatype": "Integer",
                            },
                            {
                                "name": "customer_id",
                                "expression": expr("sales.customer_id"),
                                "datatype": "Integer",
                            },
                            {
                                "name": "amount",
                                "expression": expr("sales.amount"),
                                "datatype": "Decimal",
                            },
                            {
                                "name": "sold_at",
                                "expression": expr("sales.sold_at"),
                                "datatype": "DateTime",
                                "dimension": {"is_time": True},
                            },
                            {
                                "name": "sale_year",
                                "expression": expr("EXTRACT(YEAR FROM sales.sold_at)"),
                                "datatype": "Integer",
                                "dimension": {},
                            },
                        ],
                    },
                    {
                        "name": "customer",
                        "source": "warehouse.public.customer",
                        "primary_key": ["customer_id"],
                        "fields": [
                            {
                                "name": "customer_id",
                                "expression": expr("customer.customer_id"),
                                "datatype": "Integer",
                            },
                            {
                                "name": "country",
                                "expression": expr("customer.country"),
                                "datatype": "String",
                            },
                        ],
                    },
                ],
                "relationships": [
                    {
                        "name": "sales_to_customer",
                        "from": "sales",
                        "to": "customer",
                        "from_columns": ["customer_id"],
                        "to_columns": ["customer_id"],
                    }
                ],
                "metrics": [
                    {"name": "revenue", "expression": expr("SUM(sales.amount)")},
                    {"name": "sale_count", "expression": expr("COUNT(sales.sale_id)")},
                    {"name": "buyers", "expression": expr("COUNT(DISTINCT sales.customer_id)")},
                    {
                        "name": "avg_per_buyer",
                        "expression": expr(
                            "SUM(sales.amount) / COUNT(DISTINCT customer.customer_id)"
                        ),
                    },
                ],
            }
        ],
    }


# ── import ───────────────────────────────────────────────────────────────────


def test_datasets_become_entities_with_their_source_and_key() -> None:
    result = from_osi(_document(), connection="wh")
    sales = next(e for e in result.entities if e.name == "sales")
    assert sales.source.table == "warehouse.public.sales"
    assert sales.source.connection == "wh"
    assert sales.primary_key == ["sale_id"]
    assert {f.name for f in sales.fields} == {"sale_id", "customer_id", "amount", "sold_at"}


def test_a_computed_field_becomes_a_dimension_not_a_column() -> None:
    """`EXTRACT(YEAR FROM ...)` is not a physical column; importing it as one would
    put a name in the sql_guard allow-list that the warehouse does not have."""
    sales = next(e for e in from_osi(_document(), connection="wh").entities if e.name == "sales")
    assert "sale_year" not in {f.name for f in sales.fields}
    dim = next(d for d in sales.dimensions if d.name == "sale_year")
    assert dim.expr == "EXTRACT(YEAR FROM sales.sold_at)"


def test_is_time_becomes_the_default_time_field() -> None:
    sales = next(e for e in from_osi(_document(), connection="wh").entities if e.name == "sales")
    assert sales.default_time_field == "sold_at"


def test_a_relationship_arrives_with_its_cardinality_already_safe() -> None:
    """The spec defines `from` as the MANY side, which is exactly what the compiler
    needs — so an imported join is the safe kind, not the inferred kind, which is
    often wrong."""
    sales = next(e for e in from_osi(_document(), connection="wh").entities if e.name == "sales")
    join = sales.joins[0]
    assert join.right == "customer"
    assert join.relationship == "many_to_one"
    assert join.on == "sales.customer_id = customer.customer_id"


@pytest.mark.parametrize(
    ("metric", "agg", "expr_"),
    [
        ("revenue", "sum", "sales.amount"),
        ("sale_count", "count", "sales.sale_id"),
        ("buyers", "count_distinct", "sales.customer_id"),
    ],
)
def test_a_simple_aggregate_is_reconstructed_as_a_structured_metric(
    metric: str, agg: str, expr_: str
) -> None:
    sales = next(e for e in from_osi(_document(), connection="wh").entities if e.name == "sales")
    found = next(m for m in sales.metrics if m.name == metric)
    assert (found.agg, found.expr) == (agg, expr_)


def test_a_cross_dataset_metric_is_skipped_with_its_reason() -> None:
    """Half-importing it would compile to the wrong number; OSI flattens what dst
    keeps structured, and the honest move is to say so."""
    result = from_osi(_document(), connection="wh")
    assert any("avg_per_buyer" in s for s in result.skipped)
    assert all(m.name != "avg_per_buyer" for e in result.entities for m in e.metrics)


def test_the_dataset_ai_context_lands_where_generation_reads_it() -> None:
    sales = next(e for e in from_osi(_document(), connection="wh").entities if e.name == "sales")
    assert any("One row per sale." in u for u in sales.use_cases)
    assert any("transactions" in u for u in sales.use_cases)  # synonyms preserved
    assert sales.common_questions == ["How much did we sell last month?"]


def test_a_file_that_is_not_a_semantic_model_says_so() -> None:
    with pytest.raises(ValueError, match="no `semantic_model`"):
        from_osi({"version": "0.2.0", "ontology": []}, connection="wh")


def test_two_models_in_one_file_are_refused_rather_than_merged() -> None:
    doc = _document()
    doc["semantic_model"] = [doc["semantic_model"][0], doc["semantic_model"][0]]
    with pytest.raises(ValueError, match="one at a time"):
        from_osi(doc, connection="wh")


@pytest.mark.parametrize(
    ("value", "instructions", "examples"),
    [
        ("plain string", "plain string", []),
        ({"instructions": "do this"}, "do this", []),
        ({"examples": ["q1", "q2"]}, "", ["q1", "q2"]),
        ({"synonyms": ["a", "b"]}, "Also called: a, b", []),
        (None, "", []),
    ],
)
def test_ai_context_is_read_in_both_shapes_the_schema_allows(
    value: Any, instructions: str, examples: list[str]
) -> None:
    assert read_ai_context(value) == (instructions, examples)


# ── export ───────────────────────────────────────────────────────────────────


def _entities() -> list[SharedEntity]:
    orders = SharedEntity(
        name="orders",
        source=EntitySource(connection="wh", table="db.orders"),
        grain="one row per order",
        use_cases=["Use for revenue."],
        common_questions=["What did we sell?"],
        default_time_field="created_at",
        primary_key=["order_id"],
        fields=[
            Field(name="order_id", type="integer"),
            Field(name="customer_id", type="integer"),
            Field(name="amount", type="number"),
            Field(name="created_at", type="timestamp"),
            Field(name="buyer_email", type="string"),
        ],
        metrics=[Metric(name="revenue", agg="sum", expr="orders.amount")],
        joins=[
            SharedJoin(
                right="customers",
                on="orders.customer_id = customers.customer_id",
                relationship="many_to_one",
            )
        ],
    )
    customers = SharedEntity(
        name="customers",
        source=EntitySource(connection="wh", table="db.customers"),
        fields=[Field(name="customer_id", type="integer")],
    )
    return [orders, customers]


def test_export_writes_the_judgment_into_the_slot_the_spec_reserves() -> None:
    doc, _skipped = to_osi(_entities(), name="sales", dialect="duckdb")
    dataset = next(d for d in doc["semantic_model"][0]["datasets"] if d["name"] == "orders")
    assert "one row per order" in dataset["ai_context"]["instructions"]
    assert dataset["ai_context"]["examples"] == ["What did we sell?"]


def test_export_marks_the_time_field() -> None:
    doc, _skipped = to_osi(_entities(), name="sales", dialect="duckdb")
    fields = next(d for d in doc["semantic_model"][0]["datasets"] if d["name"] == "orders")[
        "fields"
    ]
    assert next(f for f in fields if f["name"] == "created_at")["dimension"] == {"is_time": True}


def test_export_writes_the_relationship_many_side_first() -> None:
    doc, _skipped = to_osi(_entities(), name="sales", dialect="duckdb")
    rel = doc["semantic_model"][0]["relationships"][0]
    assert (rel["from"], rel["to"]) == ("orders", "customers")
    assert (rel["from_columns"], rel["to_columns"]) == (["customer_id"], ["customer_id"])


def test_an_undeclared_relationship_is_skipped_rather_than_guessed() -> None:
    """OSI relationships are directional by definition; there is no way to write
    "unknown cardinality", and guessing is how a join fan-out silently multiplies
    a metric."""
    entities = _entities()
    entities[0].joins[0].relationship = None
    doc, skipped = to_osi(entities, name="sales", dialect="duckdb")
    assert "relationships" not in doc["semantic_model"][0]
    assert any("no declared relationship" in s for s in skipped)


def test_definitions_ride_along_in_the_models_ai_context() -> None:
    definitions = [
        Definition(term="active", body="ordered in 30d"),
        Definition(
            term="churn",
            body="two meanings",
            status="ambiguous",
            possible_mappings=["billing - finance", "usage - product"],
        ),
    ]
    doc, _skipped = to_osi(_entities(), name="sales", dialect="duckdb", definitions=definitions)
    context = doc["semantic_model"][0]["ai_context"]["instructions"]
    assert "active: ordered in 30d" in context
    assert "AMBIGUOUS TERM 'churn'" in context  # the warning must survive the border


def test_bigquery_expressions_are_written_in_bigquerys_dialect_slot() -> None:
    doc, _skipped = to_osi(_entities(), name="sales", dialect="bigquery")
    field = doc["semantic_model"][0]["datasets"][0]["fields"][0]
    assert field["expression"]["dialects"][0]["dialect"] == "BIGQUERY"


# ── round trip ───────────────────────────────────────────────────────────────


def test_dst_survives_a_round_trip_through_the_open_format() -> None:
    doc, skipped = to_osi(_entities(), name="sales", dialect="duckdb")
    assert not skipped
    back = from_osi(doc, connection="wh")
    assert {e.name for e in back.entities} == {"orders", "customers"}
    orders = next(e for e in back.entities if e.name == "orders")
    assert orders.source.table == "db.orders"
    assert orders.default_time_field == "created_at"
    assert orders.joins[0].relationship == "many_to_one"
    revenue = next(m for m in orders.metrics if m.name == "revenue")
    assert (revenue.agg, revenue.expr) == ("sum", "orders.amount")
    assert not back.skipped


def test_the_second_lap_loses_nothing_more_than_the_first() -> None:
    """Whatever the format cannot carry is lost once, at the first border — a
    translation that kept degrading each pass would be unusable for interchange."""
    once = from_osi(_document(), connection="wh")
    doc, _skipped = to_osi(once.entities, name="retail", dialect="duckdb")
    twice = from_osi(doc, connection="wh")
    assert len(twice.entities) == len(once.entities)
    assert sum(len(e.metrics) for e in twice.entities) == sum(len(e.metrics) for e in once.entities)
    assert sum(len(e.joins) for e in twice.entities) == sum(len(e.joins) for e in once.entities)
    assert not twice.skipped
