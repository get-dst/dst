"""Option sets: the closed sets a typed decision resolves a question over,
built from what dst already owns. Keys are the ledger's slot kinds."""

from __future__ import annotations

from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.option_sets import option_sets


def _model() -> SemanticModel:
    orders = Entity(
        name="orders",
        source=EntitySource(connection="wh", table="orders"),
        fields=[
            Field(name="amount", type="number"),
            Field(name="status", type="string"),
            Field(name="country", type="string"),
        ],
        dimensions=[Dimension(name="country", description="ISO code")],
        metrics=[Metric(name="revenue", agg="sum", expr="orders.amount", description="money")],
    )
    refunds = Entity(
        name="refunds",
        source=EntitySource(connection="wh", table="refunds"),
        fields=[Field(name="amount", type="number"), Field(name="country", type="string")],
        dimensions=[Dimension(name="country")],
        metrics=[Metric(name="revenue", agg="sum", expr="refunds.amount")],
    )
    return SemanticModel(
        lens="l",
        dialect="duckdb",
        entities=[orders, refunds],
        definitions=[
            Definition(term="large_order", body="over 1000", sql_expr="orders.amount > 1000"),
            Definition(term="prose_only", body="no sql"),
        ],
    )


def _profile(table: str, **col_kw: object) -> TableProfile:
    return TableProfile(
        connection="wh",
        table=table,
        columns=[
            ColumnProfile(name="status", type="VARCHAR", **col_kw),  # type: ignore[arg-type]
        ],
    )


def test_option_sets_are_keyed_by_ledger_slot_and_qualified_on_collision() -> None:
    complete = _profile("orders", top_values=["shipped", "open"], values_complete=True)
    sets = option_sets(_model(), [complete])
    assert {o.name for o in sets["metric"]} == {"orders.revenue", "refunds.revenue"}
    assert sets["metric"][0].description.startswith("SUM(orders.amount) — money")
    assert [o.name for o in sets["definition"]] == ["large_order"]  # prose-only never a choice
    assert {o.name for o in sets["dimension"]} == {"orders.country", "refunds.country"}
    assert [o.name for o in sets["grain"]] == ["hour", "day", "week", "month", "quarter", "year"]
    assert [o.name for o in sets["filter_value:status"]] == ["shipped", "open"]


def test_incomplete_dictionaries_offer_no_value_options() -> None:
    partial = _profile("orders", top_values=["shipped", "open"], values_complete=False)
    sets = option_sets(_model(), [partial])
    assert not any(k.startswith("filter_value:") for k in sets)
