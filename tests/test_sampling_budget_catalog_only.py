"""A table over the sampling budget is catalog-only, and says so where values are asked.

The failure: a table whose sampling read was over ``sample_bytes_budget`` kept
its catalog profile, with no value dictionaries and no record of why. Describe
listed its fields like any other, and a question naming one of its values was
asked to supply it as a binding with nothing saying dst holds no values for
that table. Pinned: the profile records the reason, describe carries it on the
entity, and a value clarification on the entity's columns says why.
"""

from __future__ import annotations

from services.connectors.sampling import SampleBudgetExceeded, sample_table
from services.contracts.fakes import ScriptedDecider
from services.contracts.profile import (
    ColumnProfile,
    ColumnSampleSpec,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.lenses.describe import describe_model
from services.lenses.profiler import sample_profiles
from services.runtime import value_guard
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver

OVER = "dry run estimates 9000000000 bytes, over the 2000000000-byte sampling budget"

MODEL = SemanticModel(
    lens="ops",
    dialect="duckdb",
    entities=[
        Entity(
            name="shipments",
            source=EntitySource(connection="wh", table="ops.shipments"),
            fields=[
                Field(name="shipment_id", type="integer"),
                Field(name="carrier", type="string"),
            ],
            dimensions=[Dimension(name="carrier")],
            metrics=[Metric(name="shipments", agg="count", expr="shipments.shipment_id")],
        )
    ],
)


def _over_budget(sql: str) -> QueryResult:
    raise SampleBudgetExceeded(OVER)


def _catalog() -> TableProfile:
    return TableProfile(
        connection="wh",
        table="ops.shipments",
        row_count=10**9,
        columns=[
            ColumnProfile(name="shipment_id", type="BIGINT"),
            ColumnProfile(name="carrier", type="VARCHAR"),
        ],
    )


def test_an_over_budget_table_records_why_it_is_catalog_only() -> None:
    spec = TableSampleSpec(
        table="ops.shipments",
        row_count=10**9,
        columns=[ColumnSampleSpec(name="carrier", type="VARCHAR", matchable=True)],
    )
    got = sample_table(
        _over_budget,
        spec,
        connection="wh",
        dialect="duckdb",
        max_rows=100,
        max_distinct_for_enum=25,
    )
    assert got is not None and got.source == "catalog"
    assert got.unsampled is not None and OVER in got.unsampled


class _Budgeted:
    """A connector whose every sampling read is over its budget."""

    kind = "duckdb"

    def sample_profile(self, tables, *, max_rows, max_distinct_for_enum):  # type: ignore[no-untyped-def]
        return [
            p
            for spec in tables
            if (
                p := sample_table(
                    _over_budget,
                    spec,
                    connection="wh",
                    dialect="duckdb",
                    max_rows=max_rows,
                    max_distinct_for_enum=max_distinct_for_enum,
                )
            )
            is not None
        ]


def _profiles() -> list[TableProfile]:
    base = _catalog()
    merged = sample_profiles(_Budgeted(), [base], matchable=frozenset({"ops.shipments.carrier"}))
    return [merged.get(base.table, base)]


def test_the_reason_reaches_the_stored_profile_and_the_entity() -> None:
    (profile,) = _profiles()
    assert profile.source == "catalog" and profile.unsampled and OVER in profile.unsampled
    assert value_guard.entity_unsampled(MODEL, [profile]) == {
        "shipments": f"its table ops.shipments is {profile.unsampled}"
    }


def test_describe_says_the_entitys_values_cannot_bind() -> None:
    reasons = value_guard.entity_unsampled(MODEL, _profiles())
    out = describe_model("ops", display_name="Ops", description="", model=MODEL, unsampled=reasons)
    (entity,) = out.entities
    assert entity.values_note is not None
    assert "values cannot bind" in entity.values_note
    assert "ops.shipments" in entity.values_note and OVER in entity.values_note


class _Prefers(ScriptedDecider):
    def __init__(self, *prefer: str) -> None:
        super().__init__([])
        self._prefer = prefer

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        chosen = next((p for p in self._prefer if p in names), None)
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="v")


def test_a_value_clarification_on_its_columns_says_why() -> None:
    resolver = TypedResolver(
        _Prefers("aggregate", "shipments", "carrier", "stated", "="),
        unsampled=value_guard.entity_unsampled(MODEL, _profiles()),
    )
    out = TypedIntentGenerator(resolver).generate(
        question="How many shipments went with Ridgeway Freight?",
        semantic_model=MODEL,
        prose_context=[],
        dialect="duckdb",
    )
    assert out.clarification is not None, out.sql
    text = out.clarification.question
    assert "Which value of 'carrier'" in text
    assert "ops.shipments" in text and "sampling budget" in text
