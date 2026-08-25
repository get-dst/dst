"""Two metrics answering to one business word — caught at apply, not in a meeting.

The failure this pins: `definitions/revenue.md` governs `net_revenue` (captured
payments minus refunds) while `order_items.total_revenue` sits ungoverned, its own
description calling it *"the canonical revenue figure"* (gross line totals). Two
users asking in their own words get answers ~49% apart, both marked `verified`,
because the word "total" in one question matches the identifier `total_revenue`.

Nothing caught it. Not `plan`/`apply`, not certification (the certified phrasing
was unambiguous, so it passed honestly), not `dst test`, not standing evals.
Selection is *stable per phrasing*, so an eval case on any single wording passes
forever and never fires — this cannot be caught downstream, only at authoring.
"""

from __future__ import annotations

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.validate.report import validate_bundle


def _entity(name: str, *metrics: str) -> Entity:
    return Entity(
        name=name,
        source=EntitySource(connection="wh", table=f"ops.{name}"),
        fields=[Field(name="amount", type="number")],
        metrics=[Metric(name=m, agg="sum", expr=f"{name}.amount", type="simple") for m in metrics],
    )


def _codes(entities: list[Entity], definitions: list[Definition] | None = None) -> list[str]:
    model = SemanticModel(
        lens="t", dialect="duckdb", entities=entities, definitions=definitions or []
    )
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]), semantic_model=model
    )
    return [
        i.code
        for i in validate_bundle(bundle, [], []).issues
        if i.code in {"duplicate_metric", "competing_metric_claim"}
    ]


def test_the_same_metric_name_twice_is_an_error() -> None:
    """dbt refuses two models with one name. A lens accepted two `total_revenue`
    metrics in silence — a question naming it cannot resolve to one meaning."""
    codes = _codes([_entity("payments", "total_revenue"), _entity("order_items", "total_revenue")])
    assert codes == ["duplicate_metric"]


def test_lap_3s_defect_warns_naming_both_claimants() -> None:
    """A GOVERNED term and an UNGOVERNED metric sharing a business word.

    Note the shape: `net_revenue` is a definition term, `total_revenue` a metric.
    Counting only metrics finds one claimant and stays silent — which is exactly
    how the first version of this check passed its own fixture and found nothing
    on the real layer.
    """
    codes = _codes(
        [_entity("payments", "net_revenue"), _entity("order_items", "total_revenue")],
        [Definition(term="net_revenue", body="money collected, minus refunds")],
    )
    assert codes == ["competing_metric_claim"]

    # And with net_revenue governed but present ONLY as a definition:
    codes = _codes(
        [_entity("order_items", "total_revenue")],
        [Definition(term="net_revenue", body="money collected, minus refunds")],
    )
    assert codes == ["competing_metric_claim"]


def test_generic_heads_do_not_fire() -> None:
    """`order_count` and `customer_count` are two counts, not competing claims.
    A check that cries wolf on every `_count` gets switched off."""
    assert (
        _codes(
            [_entity("orders", "order_count"), _entity("customers", "customer_count")],
            [Definition(term="order_count", body="orders placed")],
        )
        == []
    )


def test_an_ungoverned_word_is_an_authoring_gap_not_a_conflict() -> None:
    """Nobody governs 'revenue' — that is a different (and lesser) problem, and
    warning about it here would bury the case that matters."""
    assert _codes([_entity("a", "net_revenue"), _entity("b", "total_revenue")]) == []


def test_when_the_author_has_ruled_on_every_claimant_it_is_silent() -> None:
    """Both governed means the author decided deliberately. Nothing to say."""
    assert (
        _codes(
            [_entity("a", "net_revenue"), _entity("b", "total_revenue")],
            [Definition(term="net_revenue", body="a"), Definition(term="total_revenue", body="b")],
        )
        == []
    )
