"""Shared-layer contracts and backward compatibility."""

from __future__ import annotations

import duckdb
import yaml

from services.certdefs import parse_definition_page, render_definition_page
from services.contracts import (
    Definition,
    Entity,
    EntitySource,
    Join,
    Metric,
    SelectSpec,
    SemanticModel,
    SharedEntity,
    SharedJoin,
    asset_content_hash,
    asset_hash,
)
from services.contracts.lens_config import LensConfig
from services.runtime.compiler import metric_sql


def _entity(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "orders",
        "source": {"connection": "wh", "table": "analytics.orders"},
    }
    base.update(over)
    return base


# ── the meta fields default inert: older payloads still validate ─────────────


def test_old_bundle_payloads_still_validate() -> None:
    old = {
        "lens": "legacy",
        "dialect": "duckdb",
        "entities": [_entity()],
        "joins": [{"left": "orders", "right": "customers", "on": "a = b"}],
        "definitions": [{"term": "x", "body": "y"}],
        # keys deleted in an earlier schema version must be ignored, not rejected
        "dbt_provenance": {"project": "p", "manifest_version": "v12", "synced_at": "t"},
    }
    sm = SemanticModel.model_validate(old)
    assert sm.entities[0].grain is None and sm.entities[0].use_cases == []
    assert sm.joins[0].relationship is None
    assert sm.definitions[0].status == "active" and sm.definitions[0].possible_mappings == []
    assert sm.shared_provenance is None


def test_old_lens_config_with_semantic_model_ref_still_validates() -> None:
    cfg = LensConfig.model_validate(
        {"name": "l", "display_name": "L", "semantic_model_ref": "draft"}
    )
    assert cfg.select.entities == [] and cfg.select.definitions == []


def test_d0_fields_round_trip() -> None:
    e = Entity.model_validate(
        _entity(
            grain="one row per order",
            use_cases=["Use for revenue", "Avoid for forecasts"],
            common_questions=["how many orders last month?"],
            metrics=[
                {
                    "name": "revenue",
                    "agg": "sum",
                    "expr": "orders.amount",
                    "filters": ["orders.status = 'completed'"],
                    "format": "currency",
                }
            ],
        )
    )
    again = Entity.model_validate(e.model_dump())
    assert again == e
    assert again.metrics[0].filters == ["orders.status = 'completed'"]


# ── selection shapes ─────────────────────────────────────────────────────────


def test_select_spec_coerces_bare_entity_names() -> None:
    spec = SelectSpec.model_validate(
        {"entities": ["customers", {"name": "orders", "metrics": ["revenue"]}]}
    )
    assert spec.entities[0].name == "customers" and spec.entities[0].metrics is None
    assert spec.entities[1].metrics == ["revenue"]
    assert spec.definitions == []  # deny-by-default: no shared terms unless selected


def test_shared_entity_carries_fk_side_joins() -> None:
    se = SharedEntity.model_validate(
        _entity(
            joins=[
                {
                    "right": "customers",
                    "on": "orders.customer_id = customers.id",
                    "relationship": "many_to_one",
                }
            ]
        )
    )
    assert se.joins[0].type == "left"  # default
    # SharedEntity minus joins is a plain Entity — the compile flattening contract
    plain = Entity.model_validate(se.model_dump(exclude={"joins"}))
    assert plain.name == "orders"


# ── authoring papercuts: the YAML `on` key, and COUNT(*) ─────────────────────


def test_join_authored_with_a_bare_on_key_survives_yaml() -> None:
    """`on:` unquoted is a YAML 1.1 BOOLEAN — safe_load returns {True: "..."}
    and `on` used to report as missing ("joins.0.on Field required"), which is
    unreadable as a diagnosis of "quote your key". Author it the obvious way."""
    body = yaml.safe_load(
        """
        name: orders
        source: {connection: wh, table: analytics.orders}
        joins:
          - right: customers
            on: orders.customer_id = customers.id
            relationship: many_to_one
        """
    )
    assert True in body["joins"][0]  # the mechanism, pinned: YAML really ate it
    assert SharedEntity.model_validate(body).joins[0].on == "orders.customer_id = customers.id"


def test_join_condition_authors_quoted_or_by_alias() -> None:
    """The two spellings that never depended on the repair: a quoted key, and
    the `condition:` alias the reference points at."""
    quoted = yaml.safe_load('{right: customers, "on": a.id = b.id}')
    assert SharedJoin.model_validate(quoted).on == "a.id = b.id"
    assert SharedJoin.model_validate({"right": "customers", "condition": "a.id = b.id"}).on == (
        "a.id = b.id"
    )
    # model-level Join too (left is explicit there), by alias and by field name
    assert Join.model_validate({"left": "o", "right": "c", "condition": "a = b"}).on == "a = b"
    assert Join(left="o", right="c", on="a = b").on == "a = b"


def test_count_metric_needs_no_expr_and_compiles_to_a_row_count() -> None:
    """`agg: count` with no `expr:` is COUNT(*) — the one aggregate that needs
    no column. Naming one instead changes the answer under NULLs."""
    m = Metric.model_validate({"name": "order_count", "agg": "count"})
    entity = Entity(name="orders", source=EntitySource(connection="wh", table="t"), metrics=[m])
    # COUNT(1) IS COUNT(*), and unlike `*` it is legal inside the filter path's
    # CASE arm — a real engine rejects COUNT(CASE WHEN … THEN * END).
    assert metric_sql(m, entity) == "COUNT(1)"
    filtered = Metric.model_validate(
        {"name": "completed", "agg": "count", "filters": ["orders.status = 'completed'"]}
    )
    sql = metric_sql(filtered, entity)
    assert sql == "COUNT(CASE WHEN orders.status = 'completed' THEN 1 END)"
    con = duckdb.connect()
    con.execute("CREATE TABLE orders AS SELECT * FROM (VALUES ('completed'),('open')) t(status)")
    assert con.execute(f"SELECT COUNT(*), {sql} FROM orders").fetchone() == (2, 1)


# ── asset hashing ────────────────────────────────────────────────────────────


def test_asset_hash_is_stable_and_order_insensitive() -> None:
    a = {"name": "orders", "fields": [{"name": "id", "type": "string"}]}
    b = {"fields": [{"type": "string", "name": "id"}], "name": "orders"}
    assert asset_hash(a) == asset_hash(b)
    assert asset_hash(a) != asset_hash({**a, "name": "customers"})


def test_asset_content_hash_survives_a_schema_addition() -> None:
    """The upgrade regression: Definition gained summary/grain/sources, every
    definition's digest moved, and the first plan after the upgrade demanded
    re-verification of certified answers whose definitions it called `unchanged`
    in the same breath. A body stored BEFORE a field existed must hash exactly as
    the same definition does now — the digest covers what the author wrote, not
    what the schema happens to serialize."""
    legacy = {"term": "repeat_customer", "body": "2+ orders", "about": "customers"}
    current = Definition.model_validate(legacy).model_dump(mode="json")
    assert set(current) - set(legacy) >= {"summary", "grain", "sources"}  # the schema grew
    assert asset_content_hash("definition", legacy) == asset_content_hash("definition", current)
    assert asset_hash(legacy) != asset_hash(current)  # …which the raw hash could not tell

    entity = {"name": "orders", "source": {"connection": "wh", "table": "t"}}
    grown = SharedEntity.model_validate(entity).model_dump(mode="json")
    assert asset_content_hash("entity", entity) == asset_content_hash("entity", grown)


def test_asset_content_hash_still_moves_when_the_meaning_does() -> None:
    """The other direction, which matters more: authoring any of those fields IS
    a meaning change (they render into the generation prompt), and so is every
    field that was always there. Certification must still be invalidated."""
    base = {"term": "repeat_customer", "body": "2+ orders"}
    digest = asset_content_hash("definition", base)
    for change in (
        {"summary": "a customer with two or more paid orders"},
        {"grain": "one row per customer"},
        {"sources": ["analytics.orders"]},
        {"body": "3+ orders"},
        {"sql_expr": "number_of_orders > 1"},
        {"status": "ambiguous"},
    ):
        assert asset_content_hash("definition", {**base, **change}) != digest, change


# ── ambiguous definitions survive the page round-trip ────────────────────────


def test_ambiguous_definition_page_round_trip() -> None:
    from services.certdefs import CertifiedDefinition

    page = render_definition_page(
        CertifiedDefinition(
            metric="workload",
            body="ASK the user: usage, projects, or deployments?",
            status="ambiguous",
            possible_mappings=["usage - product_usage", "projects - projects table"],
        ),
        minimal=True,
    )
    back = parse_definition_page(page)
    assert back.status == "ambiguous"
    assert back.possible_mappings == ["usage - product_usage", "projects - projects table"]


def test_active_definition_page_unchanged_by_new_fields() -> None:
    from services.certdefs import CertifiedDefinition

    page = render_definition_page(
        CertifiedDefinition(metric="ltv", body="prose", sql="x > 1"), minimal=True
    )
    assert "status" not in page and "possible_mappings" not in page


def test_join_relationship_and_definition_source_shared() -> None:
    j = Join(left="a", right="b", on="a.x = b.x", relationship="many_to_one")
    assert Join.model_validate(j.model_dump()) == j
    d = Definition(term="t", body="b", source="shared")
    assert d.source == "shared"
    assert Metric(name="m", agg="sum", expr="e").filters == []
    assert EntitySource(connection="c", table="t").table == "t"


def test_about_binding_round_trips_and_renders() -> None:
    """Meaning attaches to structure: Definition.about survives the page
    round-trip and shows up in the generation prompt."""
    from services.runtime.generator import serialize_model
    from services.semantic.files import definition_to_page, page_to_definition

    d = Definition(term="lifetime_value", body="prose", about="customers.customer_lifetime_value")
    back = page_to_definition(definition_to_page(d))
    assert back.about == "customers.customer_lifetime_value"
    sm = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[Entity.model_validate(_entity())],
        definitions=[d],
    )
    assert "- lifetime_value (about customers.customer_lifetime_value): prose" in (
        serialize_model(sm)
    )


def test_dangling_about_warns() -> None:
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    sm = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[Entity.model_validate(_entity())],
        definitions=[
            Definition(term="ok", body="b", about="orders"),
            Definition(term="bad", body="b", about="orders.no_such_member"),
            Definition(term="worse", body="b", about="ghosts"),
        ],
    )
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=sm,
    )
    dangling = [
        i.message
        for i in validate_bundle(bundle, [], []).issues
        if i.code == "definition_about_dangling"
    ]
    assert len(dangling) == 2
    assert any("'bad'" in m for m in dangling) and any("'worse'" in m for m in dangling)


def test_double_truth_definition_warns_unless_about_targets_a_metric() -> None:
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    sm = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity.model_validate(
                _entity(
                    fields=[{"name": "amount", "type": "number"}],
                    metrics=[{"name": "revenue", "agg": "sum", "expr": "orders.amount"}],
                )
            )
        ],
        definitions=[
            # about a plain field + own sql_expr: two independent truths -> warn
            Definition(
                term="big_order", body="b", about="orders.amount", sql_expr="orders.amount > 100"
            ),
            # about a metric: the metric owns the SQL, mirroring is construction
            Definition(
                term="revenue", body="b", about="orders.revenue", sql_expr="SUM(orders.amount)"
            ),
        ],
    )
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=sm,
    )
    doubled = [
        i.message
        for i in validate_bundle(bundle, [], []).issues
        if i.code == "definition_double_truth"
    ]
    assert len(doubled) == 1 and "'big_order'" in doubled[0]
