"""Plan-time lints for lane 2: a population_filter must bind in the compiled
query, and every entity with a metric must compile — a compile failure is a
dst defect the author meets at plan, never as a serve-time clarification."""

from __future__ import annotations

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.lenses.store import LensBundle
from services.validate.report import _population_filter_unbound, validate_bundle


def _entity(population_filter: str | None, **kw: object) -> Entity:
    return Entity(
        name="contract_events",
        source=EntitySource(connection="wh", table="raw_fieldflow.contract_events"),
        fields=[
            Field(name="event_id", type="string"),
            Field(name="_loaded_at", type="timestamp"),
            Field(name="value", type="number"),
        ],
        metrics=[Metric(name="event_count", agg="count")],
        population_filter=population_filter,
        **kw,  # type: ignore[arg-type]
    )


def _codes(entity: Entity) -> set[str]:
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=SemanticModel(lens="t", dialect="bigquery", entities=[entity]),
    )
    return {i.code for i in validate_bundle(bundle, [], []).issues}


PHYSICAL = (
    "_loaded_at = (SELECT MAX(e2._loaded_at) FROM raw_fieldflow.contract_events e2 "
    "WHERE e2.event_id = raw_fieldflow.contract_events.event_id)"
)
BOUND = (
    "_loaded_at = (SELECT MAX(e2._loaded_at) FROM raw_fieldflow.contract_events e2 "
    "WHERE e2.event_id = contract_events.event_id)"
)


def test_a_population_filter_naming_the_physical_table_fails_at_plan() -> None:
    e = _entity(PHYSICAL)
    assert _population_filter_unbound(e, "bigquery") == ["raw_fieldflow.contract_events.event_id"]
    assert "population_filter_unbound" in _codes(e)


def test_a_population_filter_on_the_entity_name_or_a_subquery_alias_binds() -> None:
    assert _population_filter_unbound(_entity(BOUND), "bigquery") == []
    assert "population_filter_unbound" not in _codes(_entity(BOUND))
    assert _population_filter_unbound(_entity("value > 0"), "bigquery") == []
    # an aliased FROM hides its table's name: `contract_events` there is the entity
    inner = "value > (SELECT AVG(x.value) FROM raw_fieldflow.contract_events x)"
    assert _population_filter_unbound(_entity(inner), "bigquery") == []


def test_every_entity_with_a_metric_must_compile_at_plan() -> None:
    fine = _entity(BOUND, default_time_field="_loaded_at")
    assert "entity_does_not_compile" not in _codes(fine)
    broken = _entity("value > (")  # does not transpile
    assert "entity_does_not_compile" in _codes(broken)


def test_a_reading_that_names_a_metric_warns_at_plan() -> None:
    from services.contracts.semantic_model import Definition

    entity = _entity(BOUND)
    entity = entity.model_copy(
        update={"metrics": [Metric(name="net_revenue", agg="sum", expr="contract_events.value")]}
    )
    d = Definition(
        term="revenue",
        body="ASK",
        status="ambiguous",
        possible_mappings=[
            "Subsidiary invoiced revenue (contract_events.net_revenue) - invoices only",
            "Group-wide consolidated revenue - all entities",
        ],
    )
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=SemanticModel(
            lens="t", dialect="bigquery", entities=[entity], definitions=[d]
        ),
    )
    issues = validate_bundle(bundle, [], []).issues
    hit = [i for i in issues if i.code == "reading_names_a_metric"]
    assert len(hit) == 1 and hit[0].severity == "warning" and "net_revenue" in hit[0].message
