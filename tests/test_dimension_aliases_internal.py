"""Dimension aliases, and columns the lens keeps but never offers.

`aliases:` on a dimension are further words that name it: "by region" groups
by `store_region`, "which region" ranks it, and a binding keyed `region`
binds it. `internal:` withholds a column from every filter, breakdown and
ranking offer while it stays readable, so a surrogate primary key (which
`hide:` refuses) is never asked about or grouped by.
"""

from __future__ import annotations

from datetime import date

from services.contracts.lens_config import LensConfig
from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.runtime.typed_resolver import TypedResolver, offered, unplaced_breakdown
from services.validate.report import validate_bundle

TODAY = date(2026, 10, 22)


def _errors(model: SemanticModel) -> list[str]:
    bundle = LensBundle(
        config=LensConfig(name="shop", display_name="Shop", connections=["wh"]),
        semantic_model=model,
    )
    return [i.code for i in validate_bundle(bundle, [], []).issues if i.severity == "error"]


class _Chooser:
    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        chosen = next(
            (p for p in self.prefer if p in names), "none" if "none" in names else names[0]
        )
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _shop(*, internal: list[str] | None = None) -> SemanticModel:
    """A bike shop chain's sales, one row per sale, keyed by a surrogate."""
    sales = Entity(
        name="sales",
        source=EntitySource(connection="wh", table="sales"),
        default_time_field="sold_on",
        primary_key=["sale_sk"],
        grain="one row per sale",
        fields=[
            Field(name="sale_sk", type="integer"),
            Field(name="sold_on", type="date"),
            Field(name="store_region", type="string"),
            Field(name="amount", type="number"),
        ],
        dimensions=[Dimension(name="store_region", aliases=["region", "area"])],
        metrics=[Metric(name="revenue", agg="sum", expr="sales.amount")],
        internal=internal or [],
    )
    return SemanticModel(lens="shop", dialect="duckdb", entities=[sales])


def _resolve(question: str, model: SemanticModel, *prefer: str, bindings=None):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Chooser(*prefer),  # type: ignore[arg-type]
        domains={"store_region": ["north", "south"]},
        bindings=bindings,
        today=TODAY,
    ).resolve(question, model)


# ── aliases ──────────────────────────────────────────────────────────────────


def test_a_breakdown_by_an_alias_groups_by_the_dimension() -> None:
    # The vote is never asked: "by region" is read off the text.
    res = _resolve("Revenue by region in May 2026", _shop(), "aggregate", "revenue")
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["store_region"]
    assert any(r.slot == "dimension" and r.provider == "question-text" for r in res.decisions)


def test_a_ranking_by_an_alias_ranks_the_dimension() -> None:
    res = _resolve("Which area had the highest revenue in May 2026?", _shop(), "ranking", "revenue")
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["store_region"]


def test_a_binding_keyed_by_an_alias_binds_the_dimension() -> None:
    res = _resolve(
        "What was revenue in May 2026?",
        _shop(),
        "aggregate",
        "revenue",
        bindings={"region": "North"},
    )
    assert res.intent is not None, res.clarification
    assert ("store_region", "north") in [(f.field, f.value) for f in res.intent.filters]


def test_an_alias_is_a_breakdown_word_the_answer_carries() -> None:
    entity = _shop().entities[0]
    assert (
        unplaced_breakdown("revenue per area", entity, ["revenue"], ["store_region"], None, [])
        is None
    )


def test_two_dimensions_claiming_one_alias_fail_plan() -> None:
    model = _shop()
    entity = model.entities[0]
    entity.fields.append(Field(name="store_area", type="string"))
    entity.dimensions.append(Dimension(name="store_area", aliases=["area"]))
    assert "dimension_alias_conflict" in _errors(model)


# ── internal columns ─────────────────────────────────────────────────────────


def test_an_internal_key_is_never_offered_even_named() -> None:
    entity = _shop(internal=["sale_sk"]).entities[0]
    options = [Option("sale_sk"), Option("store_region")]
    assert [o.name for o in offered("revenue by sale sk 7", entity, options)] == ["store_region"]


def test_a_ranking_never_groups_by_an_internal_key() -> None:
    plain = _resolve("Which sales were largest in May 2026?", _shop(), "ranking", "revenue")
    assert plain.intent is not None and plain.intent.dimensions == ["sale_sk"]
    res = _resolve(
        "Which sales were largest in May 2026?", _shop(internal=["sale_sk"]), "ranking", "revenue"
    )
    assert res.intent is None or "sale_sk" not in res.intent.dimensions
    if res.clarification is not None:
        assert "sale_sk" not in (res.clarification.options or [])
        assert "sale_sk" not in res.clarification.question


def test_an_internal_primary_key_passes_plan_where_hide_fails() -> None:
    assert "internal_unmatched" not in _errors(_shop(internal=["sale_sk"]))
    assert "dimension_alias_conflict" not in _errors(_shop(internal=["sale_sk"]))


def test_an_internal_pattern_matching_nothing_fails_plan() -> None:
    assert "internal_unmatched" in _errors(_shop(internal=["store_sk"]))


def test_the_catalog_carries_aliases_and_leaves_internal_columns_out() -> None:
    from services.lenses.describe import catalog_of

    _metrics, dims = catalog_of(_shop(internal=["sale_sk"]))
    by_name = {d.name: d for d in dims}
    assert "sale_sk" not in by_name
    assert by_name["store_region"].aliases == ["region", "area"]
    _metrics, plain = catalog_of(_shop())
    assert "sale_sk" in {d.name for d in plain}
