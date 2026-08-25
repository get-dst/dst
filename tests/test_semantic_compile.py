"""Semantic file I/O fixed point + the pure compile function."""

from __future__ import annotations

import pytest

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Definition, SampleQuery, SemanticModel
from services.contracts.shared_semantic import SelectSpec, SharedEntity
from services.project.compile import CompileError, compile_lens_model, dialect_for
from services.semantic.files import parse_semantic_files, render_semantic_files

ORDERS = SharedEntity.model_validate(
    {
        "name": "orders",
        "grain": "one row per order",
        "use_cases": ["Use for revenue reporting"],
        "common_questions": ["how many orders last month?"],
        "source": {"connection": "wh", "table": "analytics.orders"},
        "primary_key": ["order_id"],
        "fields": [
            {"name": "order_id", "type": "string"},
            {"name": "customer_id", "type": "string"},
            {"name": "amount", "type": "number"},
            {"name": "status", "type": "string"},
        ],
        "metrics": [
            {
                "name": "revenue",
                "agg": "sum",
                "expr": "orders.amount",
                "filters": ["orders.status = 'completed'"],
                "format": "currency",
            },
            {"name": "order_count", "agg": "count", "expr": "orders.order_id"},
        ],
        "joins": [
            {
                "right": "customers",
                "on": "orders.customer_id = customers.id",
                "relationship": "many_to_one",
            }
        ],
    }
)
CUSTOMERS = SharedEntity.model_validate(
    {
        "name": "customers",
        "source": {"connection": "wh", "table": "analytics.customers"},
        "fields": [{"name": "id", "type": "string"}],
    }
)
NET_REV = Definition(
    term="net_revenue", body="Revenue net of refunds.", sql_expr="orders.amount > 0"
)
WORKLOAD = Definition(
    term="workload",
    body="ASK the user which meaning.",
    status="ambiguous",
    possible_mappings=["usage - product_usage", "projects - projects"],
)
SHARED_E = {"orders": ORDERS, "customers": CUSTOMERS}
SHARED_D = {"net_revenue": NET_REV, "workload": WORKLOAD}


def _config(**select: object) -> LensConfig:
    return LensConfig(
        name="board",
        display_name="Board",
        connections=["wh"],
        select=SelectSpec.model_validate(select),
    )


def _compile(config: LensConfig, **over: object):
    kwargs: dict[str, object] = dict(
        config=config,
        shared_entities=SHARED_E,
        shared_definitions=SHARED_D,
        local_definitions=[],
        use_when=["board numbers"],
        sample_queries=[SampleQuery(question="q", sql="SELECT 1")],
        dialect="duckdb",
        asset_hashes={"entity/orders": "h1", "definition/net_revenue": "h2"},
    )
    kwargs.update(over)
    return compile_lens_model(**kwargs)  # type: ignore[arg-type]


# ── file I/O fixed point ─────────────────────────────────────────────────────


def test_semantic_files_round_trip() -> None:
    files = render_semantic_files([ORDERS, CUSTOMERS], [NET_REV, WORKLOAD])
    assert set(files) == {
        "semantic/entities/orders.yaml",
        "semantic/entities/customers.yaml",
        "semantic/definitions/net-revenue.md",
        "semantic/definitions/workload.md",
    }
    entities, definitions = parse_semantic_files(files)
    assert entities["orders"] == ORDERS and entities["customers"] == CUSTOMERS
    assert definitions["net_revenue"] == NET_REV
    assert definitions["workload"].status == "ambiguous"
    assert definitions["workload"].possible_mappings == WORKLOAD.possible_mappings
    # render ∘ parse ∘ render is the identity
    assert render_semantic_files(list(entities.values()), list(definitions.values())) == files


def test_malformed_semantic_file_names_the_path() -> None:
    with pytest.raises(ValueError, match="semantic/entities/broken.yaml"):
        parse_semantic_files({"semantic/entities/broken.yaml": "name: [unclosed"})


# ── compile ──────────────────────────────────────────────────────────────────


def test_full_entity_and_metric_subset() -> None:
    cfg = _config(entities=[{"name": "orders", "metrics": ["revenue"]}, "customers"])
    model, warnings = _compile(cfg)
    orders = next(e for e in model.entities if e.name == "orders")
    assert [m.name for m in orders.metrics] == ["revenue"]
    assert orders.metrics[0].filters == ["orders.status = 'completed'"]
    assert orders.grain == "one row per order"
    assert warnings == []
    join = model.joins[0]
    assert (join.left, join.right, join.relationship) == ("orders", "customers", "many_to_one")


def test_metric_subset_records_exclusions() -> None:
    """Selection is a visible boundary: the names a subset DROPS
    land on the compiled model so the runtime can refuse them deterministically —
    and their full SHAPES ride along so the shape guard can refuse the same
    metric rebuilt from raw columns."""
    model, _ = _compile(_config(entities=[{"name": "orders", "metrics": ["revenue"]}]))
    assert model.excluded_metrics == ["order_count"]
    shapes = model.excluded_metric_shapes
    assert list(shapes) == ["orders"] and [m.name for m in shapes["orders"]] == ["order_count"]
    assert shapes["orders"][0].agg == "count" and shapes["orders"][0].expr == "orders.order_id"
    # a full selection excludes nothing
    model, _ = _compile(_config(entities=["orders"]))
    assert model.excluded_metrics == []
    assert model.excluded_metric_shapes == {}
    # a pre-field bundle still validates (the field defaults)
    dumped = model.model_dump(mode="json")
    del dumped["excluded_metrics"]
    del dumped["excluded_metric_shapes"]
    validated = SemanticModel.model_validate(dumped)
    assert validated.excluded_metrics == [] and validated.excluded_metric_shapes == {}


def test_metric_kept_on_another_entity_is_not_excluded() -> None:
    """Dropped on one entity but still defined on a selected sibling → the lens
    CAN serve it; refusing would be a false boundary."""
    twin = SharedEntity.model_validate(
        {
            "name": "archive_orders",
            "source": {"connection": "wh", "table": "analytics.archive_orders"},
            "fields": [{"name": "order_id", "type": "string"}],
            "metrics": [{"name": "order_count", "agg": "count", "expr": "archive_orders.order_id"}],
        }
    )
    cfg = _config(entities=[{"name": "orders", "metrics": ["revenue"]}, "archive_orders"])
    model, _ = _compile(cfg, shared_entities={**SHARED_E, "archive_orders": twin})
    assert model.excluded_metrics == []
    assert model.excluded_metric_shapes == {}  # same kept-name rule as the names


def test_star_selects_every_entity() -> None:
    model, _ = _compile(_config(entities=["*"]))
    assert {e.name for e in model.entities} == {"orders", "customers"}


def test_join_to_unselected_entity_drops_with_warning() -> None:
    model, warnings = _compile(_config(entities=["orders"]))
    assert model.joins == []
    assert warnings == ["join orders -> customers dropped: 'customers' is not selected"]


def test_unknown_entity_and_metric_error() -> None:
    with pytest.raises(CompileError, match="unknown entity 'ghosts'"):
        _compile(_config(entities=["ghosts"]))
    with pytest.raises(CompileError, match="unknown metric"):
        _compile(_config(entities=[{"name": "orders", "metrics": ["nope"]}]))
    with pytest.raises(CompileError, match="unknown definition 'ghost_term'"):
        _compile(_config(definitions=["ghost_term"]))


def test_shared_definitions_restamped_and_star() -> None:
    model, _ = _compile(_config(definitions=["net_revenue"]))
    assert model.definitions[0].source == "shared"
    model, _ = _compile(_config(definitions=["*"]))
    assert {d.term for d in model.definitions} == {"net_revenue", "workload"}
    ambiguous = next(d for d in model.definitions if d.term == "workload")
    assert ambiguous.status == "ambiguous"


def test_shared_local_term_conflict_is_an_error() -> None:
    local = Definition(term="net_revenue", body="my own take")
    with pytest.raises(CompileError, match="both in the shared layer"):
        _compile(_config(definitions=["net_revenue"]), local_definitions=[local])
    # a local term that does NOT collide simply appends
    model, _ = _compile(
        _config(definitions=["net_revenue"]),
        local_definitions=[Definition(term="board_margin", body="local nuance")],
    )
    assert {d.term for d in model.definitions} == {"net_revenue", "board_margin"}


def test_provenance_records_only_consumed_assets() -> None:
    model, _ = _compile(_config(entities=["orders", "customers"], definitions=["net_revenue"]))
    assert model.shared_provenance is not None
    assert model.shared_provenance.assets == {
        "entity/orders": "h1",
        "entity/customers": "",
        "definition/net_revenue": "h2",
    }
    assert model.use_when == ["board numbers"]
    assert model.sample_queries[0].question == "q"


# ── ambiguous options tailored per lens ──────────────────────────────────────


def _value(mappings: list[str]) -> Definition:
    return Definition(
        term="value", body="ASK: which value?", status="ambiguous", possible_mappings=mappings
    )


def _compile_value(mappings: list[str], **select: object):
    select.setdefault("entities", ["orders"])
    select.setdefault("definitions", ["value"])
    return _compile(
        _config(**select),
        shared_definitions={"value": _value(mappings)},
        asset_hashes={},
    )


def test_unservable_mapping_dropped_with_warning() -> None:
    # orders.amount (field) and orders.revenue (metric) resolve; properties.* doesn't.
    model, warnings = _compile_value(
        [
            "order amount - orders.amount",
            "revenue metric - orders.revenue",
            "property value - properties.assessed_value",
        ]
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "ambiguous"
    assert value.possible_mappings == [
        "order amount - orders.amount",
        "revenue metric - orders.revenue",
    ]
    assert (
        "'value': mapping 'property value - properties.assessed_value' not in this "
        "lens's model — dropped from clarification options" in warnings
    )


def test_single_surviving_mapping_auto_resolves_to_active() -> None:
    model, warnings = _compile_value(
        ["order amount - orders.amount", "property value - properties.assessed_value"]
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "active"
    assert value.about == "orders.amount"
    assert value.body == "ASK: which value?"  # body preserved
    assert value.possible_mappings == []
    assert any("dropped from clarification options" in w for w in warnings)
    assert any("auto-resolved to active (about orders.amount)" in w for w in warnings)


def test_prose_only_mappings_never_dropped() -> None:
    # No " - " separator, and a " - " tail that isn't entity.member: both are prose,
    # kept as-is — and their presence blocks the single-survivor auto-resolve.
    model, warnings = _compile_value(
        [
            "order amount - orders.amount",
            "whatever the CFO means (ask finance)",
            "gut feel - ask the CFO",
        ]
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "ambiguous"
    assert value.possible_mappings == [
        "order amount - orders.amount",
        "whatever the CFO means (ask finance)",
        "gut feel - ask the CFO",
    ]
    assert [w for w in warnings if "'value'" in w] == []


def test_zero_surviving_mappings_keeps_original_options() -> None:
    # Both refs are structural and neither fits this lens: better to ask with stale
    # options than to guess — the original list survives, one warning, no drop spam.
    model, warnings = _compile_value(
        ["usage - product_usage", "projects - projects"],
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "ambiguous"
    assert value.possible_mappings == ["usage - product_usage", "projects - projects"]
    assert [w for w in warnings if "'value'" in w] == [
        "'value': no possible_mappings fit this lens's model — "
        "keeping the original clarification options"
    ]


def test_metric_subset_narrows_the_options_too() -> None:
    # The lens subsets orders to just revenue: a mapping onto order_count is no
    # longer servable here, so the term auto-resolves to the one metric it kept.
    model, warnings = _compile_value(
        ["revenue - orders.revenue", "order count - orders.order_count"],
        entities=[{"name": "orders", "metrics": ["revenue"]}],
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "active" and value.about == "orders.revenue"
    assert any("auto-resolved" in w for w in warnings)


def test_demo_two_mapping_case_unchanged() -> None:
    # The jaffle teaching example serves both meanings — nothing drops, still asks.
    from services.lenses.demo import jaffle_customer_value_config, jaffle_shared_assets

    entities, definitions = jaffle_shared_assets()
    model, warnings = compile_lens_model(
        config=jaffle_customer_value_config(),
        shared_entities={e.name: e for e in entities},
        shared_definitions={d.term: d for d in definitions},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
    )
    value = next(d for d in model.definitions if d.term == "value")
    assert value.status == "ambiguous"
    assert value.possible_mappings == [
        "lifetime value - customers.customer_lifetime_value",
        "order amount - orders.amount",
    ]
    assert not [w for w in warnings if "'value'" in w]


def test_dialect_mapping() -> None:
    assert dialect_for("duckdb") == "duckdb"
    with pytest.raises(CompileError, match="no SQL dialect"):
        dialect_for("notion")


def test_duplicate_shared_names_raise() -> None:
    """Two files claiming one name must error, not last-file-win."""
    files = render_semantic_files([ORDERS], [NET_REV])
    dup_entity = {
        **files,
        "semantic/entities/orders-copy.yaml": files["semantic/entities/orders.yaml"],
    }
    with pytest.raises(ValueError, match="defined in both"):
        parse_semantic_files(dup_entity)
    dup_def = {
        **files,
        "semantic/definitions/net-revenue-2.md": files["semantic/definitions/net-revenue.md"],
    }
    with pytest.raises(ValueError, match="defined in both"):
        parse_semantic_files(dup_def)


def test_plan_canonicalizes_lean_files() -> None:
    """A hand-authored lean file (defaults omitted) must plan
    as 'unchanged' against the DB's canonical render — no phantom diffs."""
    from services.project.plan import plan_semantic

    canonical = render_semantic_files([CUSTOMERS], [])
    path = "semantic/entities/customers.yaml"
    lean = (
        "name: customers\nsource: {connection: wh, table: analytics.customers}\n"
        "fields: [{name: id, type: string}]\n"
    )
    plans = plan_semantic({path: canonical[path]}, {path: lean})
    assert [(p.path, p.status) for p in plans] == [(path, "unchanged")]
    # a REAL edit still shows as an update
    edited = lean.replace("analytics.customers", "analytics.customers_v2")
    plans = plan_semantic({path: canonical[path]}, {path: edited})
    assert plans[0].status == "update"


def test_missing_connection_names_the_file_that_needs_the_edit(monkeypatch) -> None:
    """A lens with no resolvable warehouse used to say "declare one in dst.yaml"
    in BOTH cases. When `connections:` is simply absent — the commoner case — the
    file to edit is the lens's own lens.yaml, and every author was sent to a file
    that was already correct."""
    from services.project import apply as apply_mod

    empty = LensConfig(name="sales", display_name="Sales", connections=[])
    with pytest.raises(CompileError) as exc:
        apply_mod._lens_dialect(None, empty)  # no connections -> the session is never read
    assert "lenses/sales/lens.yaml" in str(exc.value) and "connections:" in str(exc.value)

    named = LensConfig(name="sales", display_name="Sales", connections=["wh"])
    monkeypatch.setattr(apply_mod.connection_store, "get_connection", lambda _s, _n: None)
    with pytest.raises(CompileError) as exc:
        apply_mod._lens_dialect(None, named)
    # ...but a name that IS listed and does not resolve really is dst.yaml's.
    assert "dst.yaml" in str(exc.value) and "'wh'" in str(exc.value)
