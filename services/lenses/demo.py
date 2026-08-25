"""Demo lens: `customer_value` over the jaffle DuckDB warehouse.

The demo takes the shared semantic layer path end to end: the entities and
definitions are authored as shared assets (`jaffle_shared_assets`) and the
bundle's embedded SemanticModel is BUILT by `compile_lens_model` from the
lens's `select` — the exact path `dst apply` takes. `dst demo` seeds
the assets into semantic_asset alongside the lens so its compile provenance
matches the store.
"""

from __future__ import annotations

from services.contracts.lens_config import LensConfig, ModelConfig
from services.contracts.semantic_model import (
    Definition,
    EntitySource,
    Field,
    Metric,
    SampleQuery,
    SemanticModel,
)
from services.contracts.shared_semantic import SelectEntity, SelectSpec, SharedEntity, SharedJoin
from services.lenses.store import LensBundle
from services.project.compile import (
    compile_lens_model,
    shared_definition_hash,
    shared_entity_hash,
)

LENS_NAME = "customer_value"
CONNECTION = "jaffle"


def jaffle_shared_assets() -> tuple[list[SharedEntity], list[Definition]]:
    """The jaffle shared layer: customers + orders entities, two governed terms."""
    entities = [
        SharedEntity(
            name="customers",
            description="One row per customer.",
            source=EntitySource(connection=CONNECTION, table="customers"),
            primary_key=["customer_id"],
            fields=[
                Field(name="customer_id", type="integer", description="Customer PK."),
                Field(name="customer_name", type="string"),
                Field(name="number_of_orders", type="integer"),
                Field(
                    name="customer_lifetime_value",
                    type="number",
                    description="Total historical revenue from the customer (USD).",
                ),
                Field(name="first_order_date", type="date"),
                Field(name="most_recent_order_date", type="date"),
            ],
            metrics=[
                Metric(name="total_clv", agg="sum", expr="customers.customer_lifetime_value"),
                Metric(name="avg_clv", agg="avg", expr="customers.customer_lifetime_value"),
            ],
        ),
        SharedEntity(
            name="orders",
            description="One row per order.",
            source=EntitySource(connection=CONNECTION, table="orders"),
            default_time_field="order_date",
            primary_key=["order_id"],
            fields=[
                Field(name="order_id", type="integer"),
                Field(name="customer_id", type="integer"),
                Field(name="order_date", type="date"),
                Field(name="status", type="string"),
                Field(name="amount", type="number", description="Order total (USD)."),
            ],
            metrics=[
                Metric(name="revenue", agg="sum", expr="orders.amount", format="currency"),
                Metric(name="order_count", agg="count", expr="orders.order_id"),
                # The teaching example for compound metrics: a ratio over siblings.
                Metric(
                    name="average_order_value",
                    type="ratio",
                    numerator="revenue",
                    denominator="order_count",
                    format="currency",
                    description="Revenue divided by order count.",
                ),
            ],
            # Joins live on the FK side: many orders -> one customer.
            joins=[
                SharedJoin(
                    right="customers",
                    on="customers.customer_id = orders.customer_id",
                    relationship="many_to_one",
                )
            ],
        ),
    ]
    definitions = [
        Definition(
            term="lifetime_value",
            about="customers.customer_lifetime_value",
            body=(
                "Lifetime value is a customer's total realized order revenue in USD "
                "- the sum of their order amounts to date, surfaced as "
                "customers.customer_lifetime_value. Realized value only: never "
                "projected or modeled forward. Note that returned orders still "
                "count toward it - the jaffle mart does not net them out."
            ),
        ),
        Definition(
            term="repeat_customer",
            body="A repeat customer has number_of_orders > 1.",
            sql_expr="customers.number_of_orders > 1",
        ),
        # The teaching example for the glossary: an ambiguous term makes
        # dst ask which meaning is intended instead of guessing.
        Definition(
            term="value",
            body=(
                "ASK the user: 'value' is ambiguous in this dataset - lifetime value "
                "(total historical revenue per customer) or order amount (a single "
                "order's total)?"
            ),
            status="ambiguous",
            possible_mappings=[
                "lifetime value - customers.customer_lifetime_value",
                "order amount - orders.amount",
            ],
        ),
    ]
    return entities, definitions


def _demo_model_config() -> ModelConfig:
    """The scaffolded lens names no model, so it follows the install's own smart
    tier (BYOK: an ollama-only install must not get a claude-named demo lens it
    can't serve). That used to need a manual tier lookup here; since the
    contract default IS "unset" it is simply the default."""
    return ModelConfig(temperature=0.0, max_rows_to_compose=200)


def jaffle_customer_value_config() -> LensConfig:
    return LensConfig(
        name=LENS_NAME,
        display_name="Customer Value",
        description="Customer lifetime value and order activity over the jaffle dataset.",
        connections=[CONNECTION],
        select=SelectSpec(
            entities=[SelectEntity(name="customers"), SelectEntity(name="orders")],
            definitions=["lifetime_value", "repeat_customer", "value"],
        ),
        model=_demo_model_config(),
        # No "use X for value questions" steer here: 'value' is the demo's
        # AMBIGUOUS teaching term — an instruction resolving it would kill the
        # clarify moment the scaffold exists to showcase.
        # And no restating repeat_customer's sql_expr here either: an
        # instruction that mirrors a definition keeps steering answers after
        # the governed definition changes, silently shadowing it.
        instructions="Select explicit columns.",
    )


def jaffle_customer_value_bundle() -> LensBundle:
    return LensBundle(
        config=jaffle_customer_value_config(),
        semantic_model=jaffle_customer_value(),
    )


def jaffle_customer_value() -> SemanticModel:
    entities, definitions = jaffle_shared_assets()
    model, _warnings = compile_lens_model(
        config=jaffle_customer_value_config(),
        shared_entities={e.name: e for e in entities},
        shared_definitions={d.term: d for d in definitions},
        local_definitions=[],
        use_when=[],
        sample_queries=[
            # Deliberately definition-free: a sample that embeds a definition's
            # logic trips the stale-sample gate as soon as that definition is
            # edited, masking the certified divergence — and exemplars should
            # teach shape, not restate governed meaning the definitions own.
            SampleQuery(
                question="How many orders were placed in total?",
                sql="SELECT count(*) AS n FROM orders",
            ),
        ],
        dialect="duckdb",
        asset_hashes={
            **{f"entity/{e.name}": shared_entity_hash(e) for e in entities},
            **{f"definition/{d.term}": shared_definition_hash(d) for d in definitions},
        },
    )
    return model
