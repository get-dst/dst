"""Phase C metric-layer: deterministic compiler, intent generator, pipeline integration."""

from __future__ import annotations

import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.query_intent import IntentFilter, IntentOrder, QueryIntent
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SemanticModel,
)
from services.lenses.demo import jaffle_customer_value
from services.runtime.answer import AnswerComposer
from services.runtime.compiler import CompileError, compile_intent, metric_sql
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.intent_generator import IntentSQLGenerator
from services.runtime.pipeline import run_query


def _conn() -> DuckDBConnector:
    return DuckDBConnector(settings.duckdb_jaffle_path)


# ── compiler (pure) ──────────────────────────────────────────────────────────


def test_compile_metric_only() -> None:
    sql = compile_intent(QueryIntent(metrics=["total_clv"]), jaffle_customer_value())
    low = sql.lower()
    assert "sum(" in low and "total_clv" in low
    assert "group by" not in low  # no dimensions → aggregate over all rows


def test_compile_metric_with_filter_and_dimension() -> None:
    intent = QueryIntent(
        metrics=["avg_clv"],
        dimensions=["customer_name"],
        filters=[IntentFilter(field="number_of_orders", op=">", value=1)],
        order_by=[IntentOrder(field="avg_clv", dir="desc")],
        limit=10,
    )
    sql = compile_intent(intent, jaffle_customer_value()).lower()
    assert "avg(" in sql and "group by" in sql
    assert "number_of_orders > 1" in sql
    assert "order by" in sql and "limit 10" in sql


def test_compile_rejects_unknown_metric() -> None:
    with pytest.raises(CompileError):
        compile_intent(QueryIntent(metrics=["does_not_exist"]), jaffle_customer_value())


def test_compile_rejects_unknown_filter_field() -> None:
    with pytest.raises(CompileError):
        compile_intent(
            QueryIntent(metrics=["total_clv"], filters=[IntentFilter(field="nope", value=1)]),
            jaffle_customer_value(),
        )


def test_compile_string_value_is_escaped() -> None:
    intent = QueryIntent(
        metrics=["total_clv"],
        filters=[IntentFilter(field="customer_name", op="=", value="O'Brien")],
    )
    sql = compile_intent(intent, jaffle_customer_value())
    assert "'O''Brien'" in sql  # single quote doubled, not breaking out


# ── compound metrics (ratio / derived) ───────────────────────────────────────


def _orders_entity() -> Entity:
    return Entity(
        name="orders",
        source=EntitySource(connection="jaffle", table="orders"),
        fields=[
            Field(name="order_id", type="integer"),
            Field(name="amount", type="number"),
            Field(name="status", type="string"),
        ],
        metrics=[
            Metric(name="revenue", agg="sum", expr="orders.amount"),
            Metric(name="order_count", agg="count", expr="orders.order_id"),
            Metric(
                name="completed_revenue",
                agg="sum",
                expr="orders.amount",
                filters=["orders.status = 'completed'"],
            ),
            Metric(name="aov", type="ratio", numerator="revenue", denominator="order_count"),
            Metric(name="open_revenue", type="derived", expr="{revenue} - {completed_revenue}"),
        ],
    )


def _orders_model() -> SemanticModel:
    return SemanticModel(lens="t", dialect="duckdb", entities=[_orders_entity()])


def test_metric_sql_ratio_shape() -> None:
    e = _orders_entity()
    aov = next(m for m in e.metrics if m.name == "aov")
    assert metric_sql(aov, e) == (
        "CAST(SUM(orders.amount) AS DOUBLE) / NULLIF(COUNT(orders.order_id), 0)"
    )


def test_metric_sql_derived_inlines_referenced_filters() -> None:
    e = _orders_entity()
    open_rev = next(m for m in e.metrics if m.name == "open_revenue")
    assert metric_sql(open_rev, e) == (
        "(SUM(orders.amount)) - (SUM(CASE WHEN orders.status = 'completed' THEN orders.amount END))"
    )


def test_compile_ratio_executes() -> None:
    sql = compile_intent(QueryIntent(metrics=["aov"]), _orders_model())
    got = _conn().execute(sql, read_only=True).rows[0][0]
    want = _conn().execute("SELECT SUM(amount) * 1.0 / COUNT(*) FROM orders", read_only=True)
    assert got == pytest.approx(want.rows[0][0])


def test_compound_beside_filtered_metric_gets_no_shared_where() -> None:
    # completed_revenue's filter must go CASE-inline, never into a shared WHERE
    # that would poison the ratio's operands.
    sql = compile_intent(QueryIntent(metrics=["completed_revenue", "aov"]), _orders_model())
    low = sql.lower()
    assert "where" not in low
    assert "case when" in low


def test_metric_sql_unknown_ref_raises() -> None:
    e = _orders_entity()
    ghost = Metric(name="bad", type="ratio", numerator="revenue", denominator="ghost")
    e.metrics.append(ghost)
    with pytest.raises(CompileError, match="unknown sibling metric 'ghost'"):
        metric_sql(ghost, e)


def test_metric_sql_cycle_raises() -> None:
    e = _orders_entity()
    a = Metric(name="a", type="derived", expr="{b} + 1")
    b = Metric(name="b", type="derived", expr="{a} + 1")
    e.metrics += [a, b]
    with pytest.raises(CompileError, match="defined in terms of itself"):
        metric_sql(a, e)


def test_demo_ratio_metric_compiles_and_runs() -> None:
    # The demo teaches compound metrics: orders.average_order_value = revenue/order_count.
    # No entity named — ownership resolution must pick orders over customers.
    sql = compile_intent(QueryIntent(metrics=["average_order_value"]), jaffle_customer_value())
    got = _conn().execute(sql, read_only=True).rows[0][0]
    want = _conn().execute("SELECT SUM(amount) * 1.0 / COUNT(*) FROM orders", read_only=True)
    assert got == pytest.approx(want.rows[0][0])


def test_entity_resolved_by_metric_ownership() -> None:
    # Two entities, both with metrics, no entity named — the one owning the
    # requested metric wins instead of "ambiguous entity".
    model = _orders_model()
    model.entities.append(
        Entity(
            name="customers",
            source=EntitySource(connection="jaffle", table="customers"),
            fields=[Field(name="customer_lifetime_value", type="number")],
            metrics=[Metric(name="total_clv", agg="sum", expr="customers.customer_lifetime_value")],
        )
    )
    sql = compile_intent(QueryIntent(metrics=["aov"]), model)
    assert "FROM orders" in sql


# ── intent generator ─────────────────────────────────────────────────────────


def test_intent_generator_compiles() -> None:
    gen = IntentSQLGenerator(ScriptedLLM(['{"metrics": ["total_clv"]}']))
    gq = gen.generate(
        question="total lifetime value?",
        semantic_model=jaffle_customer_value(),
        prose_context=[],
        dialect="duckdb",
    )
    assert "sum(" in gq.sql.lower()


def test_intent_generator_empty_on_uncompilable() -> None:
    gen = IntentSQLGenerator(ScriptedLLM(['{"metrics": ["bogus"]}']))
    gq = gen.generate(
        question="x",
        semantic_model=jaffle_customer_value(),
        prose_context=[],
        dialect="duckdb",
    )
    assert gq.sql == ""  # uncompilable → empty → pipeline will escalate


# ── pipeline integration ─────────────────────────────────────────────────────


def test_pipeline_metric_path_executes() -> None:
    llm = ScriptedLLM(['{"metrics": ["total_clv"]}', "The total is grounded."])
    res = run_query(
        question="total lifetime value?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=IntentSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "sum(" in res.response.sql.lower()


def test_pipeline_falls_back_to_raw_sql_when_intent_uncompilable() -> None:
    # Intent generator can't compile (needs a join) → guard rejects empty SQL →
    # escalate to raw-SQL generation, which succeeds.
    intent_llm = ScriptedLLM(['{"metrics": ["bogus"]}'])
    raw_llm = ScriptedLLM(['{"sql": "SELECT count(*) AS n FROM customers"}'])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=IntentSQLGenerator(intent_llm),
        escalate_generator=GroundedSQLGenerator(raw_llm),
        composer=AnswerComposer(ScriptedLLM(["Some customers."])),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "customers" in res.response.sql.lower()


# ── identifiers that are SQL keywords (e.g. an ecommerce `order` table) ──────


def _keyword_model(name: str, dialect: str = "duckdb") -> SemanticModel:
    """One entity named after a SQL keyword, over a physical table of the same
    name — the shape `dst apply` happily creates from an ecommerce warehouse."""
    return SemanticModel(
        lens="shop",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name=name,
                source=EntitySource(connection="wh", table=name),
                fields=[Field(name="amount", type="number"), Field(name="key", type="string")],
                metrics=[Metric(name="n", type="simple", agg="count", expr="*")],
            )
        ],
    )


@pytest.mark.parametrize("name", ["order", "group", "table", "user", "end"])
def test_keyword_entity_compiles_and_runs_against_a_real_warehouse(name: str) -> None:
    """`FROM orders AS order` is not SQL. The compiler emitted exactly that and
    every answer through the entity died on a warehouse parser error — for one of
    the commonest table names there is, which the author cannot rename."""
    import duckdb

    con = duckdb.connect()
    con.execute(f'CREATE TABLE "{name}" (amount INTEGER, "key" VARCHAR)')
    con.execute(f"INSERT INTO \"{name}\" VALUES (7, 'a'), (9, 'b')")

    sql = compile_intent(
        QueryIntent(entity=name, metrics=["n"], dimensions=["key"]), _keyword_model(name)
    )
    assert con.execute(sql).fetchall() == [("a", 1), ("b", 1)]


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery", "mysql"])
def test_keyword_entity_is_delimited_in_every_dialect(dialect: str) -> None:
    """sqlglot's generator quotes the keywords IT knows, and its set differs per
    dialect — postgres and snowflake quoted neither `order` nor `group`."""
    sql = compile_intent(
        QueryIntent(entity="order", metrics=["n"]), _keyword_model("order", dialect)
    )
    delimiter = "`" if dialect in ("mysql", "bigquery") else '"'
    assert f"{delimiter}order{delimiter}" in sql
    assert " AS order" not in sql and "FROM order " not in sql


def test_an_ordinary_name_is_still_emitted_bare() -> None:
    """Quoting unconditionally would pin a case Snowflake and BigQuery fold, and
    break lenses that work today. Only what needs delimiting gets it."""
    sql = compile_intent(QueryIntent(metrics=["total_clv"]), jaffle_customer_value())
    assert '"customers"' not in sql and "customers" in sql


# ── joins: dimensions and filters on another entity (B1) ─────────────────────


def _joined_model(relationship: str | None, *, join: bool = True) -> SemanticModel:
    """orders (metrics) + regions (a lookup), joined with *relationship*."""
    orders = Entity(
        name="orders",
        source=EntitySource(connection="jaffle", table="orders"),
        fields=[
            Field(name="order_id", type="integer"),
            Field(name="amount", type="number"),
            Field(name="region_id", type="integer"),
        ],
        metrics=[Metric(name="revenue", agg="sum", expr="orders.amount")],
    )
    regions = Entity(
        name="regions",
        source=EntitySource(connection="jaffle", table="regions"),
        fields=[Field(name="region_id", type="integer"), Field(name="region_name", type="string")],
    )
    joins = (
        [
            Join(
                left="orders",
                right="regions",
                on="orders.region_id = regions.region_id",
                relationship=relationship,  # type: ignore[arg-type]
            )
        ]
        if join
        else []
    )
    return SemanticModel(lens="t", dialect="duckdb", entities=[orders, regions], joins=joins)


def test_compile_joins_for_a_dimension_on_another_entity() -> None:
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["region_name"]),
        _joined_model("many_to_one"),
    )
    assert "JOIN regions" in sql and "orders.region_id = regions.region_id" in sql
    assert "regions.region_name" in sql and "GROUP BY" in sql


def test_compile_joins_for_a_filter_on_another_entity() -> None:
    sql = compile_intent(
        QueryIntent(
            entity="orders",
            metrics=["revenue"],
            filters=[IntentFilter(field="region_name", op="=", value="EU")],
        ),
        _joined_model("many_to_one"),
    )
    assert "JOIN regions" in sql and "regions.region_name = 'EU'" in sql


def test_compile_refuses_a_join_that_fans_the_metrics_out() -> None:
    """The defect this whole path exists to prevent: joining the many side of a
    one-to-many multiplies the base rows and inflates every additive aggregate
    — a sum can report several times its true value."""
    with pytest.raises(CompileError, match="duplicates every 'orders' row"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], dimensions=["region_name"]),
            _joined_model("one_to_many"),
        )


def test_compile_refuses_an_undeclared_relationship() -> None:
    """No declared cardinality is not evidence of safety: a foreign key is
    routinely not one-to-many in the data, so inferring the relationship is
    wrong often enough to matter."""
    with pytest.raises(CompileError, match="duplicates every 'orders' row"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], dimensions=["region_name"]),
            _joined_model(None),
        )


def test_compile_refuses_when_no_join_path_is_declared() -> None:
    with pytest.raises(CompileError, match="no declared join path"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], dimensions=["region_name"]),
            _joined_model("many_to_one", join=False),
        )


def test_compile_refuses_a_bare_name_two_joined_entities_both_carry() -> None:
    """`region_name` on two non-base entities: picking one would be a coin flip.
    (A name the BASE carries is not ambiguous — see the base-wins test below.)"""
    model = _joined_model("many_to_one")
    model.entities.append(
        Entity(
            name="territories",
            source=EntitySource(connection="jaffle", table="territories"),
            fields=[
                Field(name="region_id", type="integer"),
                Field(name="region_name", type="string"),
            ],
        )
    )
    model.joins.append(
        Join(
            left="orders",
            right="territories",
            on="orders.region_id = territories.region_id",
            relationship="many_to_one",
        )
    )
    with pytest.raises(CompileError, match="ambiguous dimension/field 'region_name'"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], dimensions=["region_name"]), model
        )


def test_a_qualified_member_disambiguates_and_needs_no_join() -> None:
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["orders.region_id"]),
        _joined_model("many_to_one"),
    )
    assert "JOIN" not in sql  # the base entity already carries it
    assert "orders.region_id" in sql


def test_the_base_entity_wins_a_bare_name_it_owns() -> None:
    """A fact table's own column is what the question means by that name; only a
    name the base does NOT carry may resolve to another entity."""
    model = _joined_model("many_to_one")
    model.entities[1].fields.append(Field(name="amount", type="number"))
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["amount"]), model
    )
    assert "orders.amount" in sql and "regions.amount" not in sql


def test_a_join_compiles_to_correct_numbers() -> None:
    """Against the real jaffle warehouse, not just a string match: revenue per
    customer through the declared orders -> customers join."""
    model = jaffle_customer_value()
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["customer_name"]), model
    )
    got = sorted(_conn().execute(sql, read_only=True).rows)
    want = sorted(
        _conn()
        .execute(
            "SELECT c.customer_name, SUM(o.amount) FROM orders o "
            "JOIN customers c ON c.customer_id = o.customer_id GROUP BY 1",
            read_only=True,
        )
        .rows
    )
    assert got == want and len(got) > 1


# ── time grain (B5) ──────────────────────────────────────────────────────────


def _timed_model(
    dialect: str = "duckdb", *, time_field: str | None = "order_date"
) -> SemanticModel:
    orders = Entity(
        name="orders",
        source=EntitySource(connection="jaffle", table="orders"),
        default_time_field=time_field,
        fields=[
            Field(name="order_id", type="integer"),
            Field(name="order_date", type="timestamp"),
            Field(name="shipped_at", type="timestamp"),
            Field(name="amount", type="number"),
        ],
        metrics=[
            Metric(name="revenue", agg="sum", expr="orders.amount"),
            Metric(name="shipped", agg="sum", expr="orders.amount", agg_time_field="shipped_at"),
        ],
    )
    return SemanticModel(lens="t", dialect=dialect, entities=[orders])


def test_grain_buckets_and_groups_by_the_period() -> None:
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], grain="month"), _timed_model()
    )
    assert "DATE_TRUNC('MONTH', orders.order_date) AS order_date_month" in sql
    assert sql.count("DATE_TRUNC") == 2  # SELECT and GROUP BY


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("duckdb", "DATE_TRUNC('MONTH', orders.order_date)"),
        ("postgres", "DATE_TRUNC('MONTH', orders.order_date)"),
        ("snowflake", "DATE_TRUNC('MONTH', orders.order_date)"),
        ("bigquery", "DATE_TRUNC(orders.order_date, MONTH)"),  # unit comes SECOND
    ],
)
def test_the_bucket_is_written_in_each_dialects_own_syntax(dialect: str, expected: str) -> None:
    """A DATE_TRUNC string would be wrong in BigQuery (argument order) and absent in
    MySQL, so the bucket is built as a sqlglot node and generated per dialect."""
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], grain="month"), _timed_model(dialect)
    )
    assert expected in sql


def test_mysql_has_no_date_trunc_and_still_gets_a_month() -> None:
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], grain="month"), _timed_model("mysql")
    )
    assert "DATE_TRUNC" not in sql.upper()
    assert "STR_TO_DATE" in sql.upper()


def test_a_metric_can_override_which_time_field_is_bucketed() -> None:
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["shipped"], grain="week"), _timed_model()
    )
    assert "orders.shipped_at" in sql and "shipped_at_week" in sql


def test_metrics_disagreeing_about_the_time_field_is_refused() -> None:
    with pytest.raises(CompileError, match="disagree about which time field"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue", "shipped"], grain="day"),
            _timed_model(),
        )


def test_grain_without_a_declared_time_field_names_the_fix() -> None:
    with pytest.raises(CompileError, match="declares no default_time_field"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], grain="month"),
            _timed_model(time_field=None),
        )


def test_a_time_field_that_is_not_a_field_is_refused() -> None:
    with pytest.raises(CompileError, match="no such field"):
        compile_intent(
            QueryIntent(entity="orders", metrics=["revenue"], grain="month"),
            _timed_model(time_field="ghost_date"),
        )


def test_the_bucket_can_be_ordered_by_its_alias() -> None:
    sql = compile_intent(
        QueryIntent(
            entity="orders",
            metrics=["revenue"],
            grain="month",
            order_by=[IntentOrder(field="order_date_month", dir="asc")],
        ),
        _timed_model(),
    )
    assert "ORDER BY order_date_month ASC" in sql


def test_grain_beside_a_dimension_groups_by_both() -> None:
    model = _timed_model()
    model.entities[0].fields.append(Field(name="status", type="string"))
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["status"], grain="month"),
        model,
    )
    assert sql.index("order_date_month") < sql.index("status")  # period leads
    assert "GROUP BY" in sql and "orders.status" in sql


def test_a_monthly_trend_computes_correct_numbers() -> None:
    """Against the real jaffle warehouse: the compiled bucket must agree with SQL
    written by hand, which is the whole point of taking the grain off the prompt."""
    sql = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], grain="month"), jaffle_customer_value()
    )
    got = sorted(_conn().execute(sql, read_only=True).rows)
    want = sorted(
        _conn()
        .execute(
            "SELECT DATE_TRUNC('MONTH', order_date), SUM(amount) FROM orders GROUP BY 1",
            read_only=True,
        )
        .rows
    )
    assert got == want and len(got) > 1


# ── joins: the METRIC's own expression reaches into another entity ────────────


def _price_model(relationship: str | None, *, join: bool = True) -> SemanticModel:
    """A metric multiplying its own column by a lookup entity's column.

    `dollars_spent = quantity * bitcoin_prices.price` applies clean, compiles to
    a SELECT over the transactions table ALONE, and dies at the warehouse on
    `Referenced table "bitcoin_prices" not found`. The reference is computable —
    over the declared many_to_one — so the compiler joins it.
    """
    txns = Entity(
        name="txns",
        source=EntitySource(connection="bank", table="txns"),
        fields=[
            Field(name="ticker", type="string"),
            Field(name="quantity", type="number"),
        ],
        metrics=[
            Metric(name="units", agg="sum", expr="txns.quantity"),
            Metric(name="dollars", agg="sum", expr="txns.quantity * prices.price"),
            Metric(name="avg_price", type="ratio", numerator="dollars", denominator="units"),
        ],
    )
    prices = Entity(
        name="prices",
        source=EntitySource(connection="bank", table="prices"),
        fields=[Field(name="ticker", type="string"), Field(name="price", type="number")],
    )
    joins = (
        [
            Join(
                left="txns",
                right="prices",
                on="txns.ticker = prices.ticker",
                relationship=relationship,  # type: ignore[arg-type]
            )
        ]
        if join
        else []
    )
    return SemanticModel(lens="t", dialect="duckdb", entities=[txns, prices], joins=joins)


def test_a_metric_expression_pulls_its_entity_into_the_from_clause() -> None:
    sql = compile_intent(
        QueryIntent(entity="txns", metrics=["dollars"], dimensions=["ticker"]),
        _price_model("many_to_one"),
    )
    assert "JOIN prices" in sql and "txns.ticker = prices.ticker" in sql
    assert "SUM(txns.quantity * prices.price)" in sql


def test_a_ratio_inherits_its_operands_reach() -> None:
    """Only the EXPANSION says avg_price touches prices — the ratio's own body
    names two sibling metrics and nothing else."""
    sql = compile_intent(
        QueryIntent(entity="txns", metrics=["avg_price"]), _price_model("many_to_one")
    )
    assert "JOIN prices" in sql


def test_a_metric_expression_across_a_fanning_join_is_still_refused() -> None:
    """Joining still has to be sound: the reference does not buy the hop."""
    with pytest.raises(CompileError, match="duplicates every 'txns' row"):
        compile_intent(QueryIntent(entity="txns", metrics=["dollars"]), _price_model("one_to_many"))


def test_a_metric_expression_with_no_declared_join_is_refused() -> None:
    with pytest.raises(CompileError, match="no declared join path"):
        compile_intent(
            QueryIntent(entity="txns", metrics=["dollars"]),
            _price_model("many_to_one", join=False),
        )


def test_a_metric_that_stays_home_joins_nothing() -> None:
    sql = compile_intent(QueryIntent(entity="txns", metrics=["units"]), _price_model("many_to_one"))
    assert "JOIN" not in sql


# ── governed definitions applied as filters (the silent-wrong class) ─────────
#
# A metric lens generates on the intent tier, whose serialize_layer prompt renders
# a definition's BODY but not its sql_expr. So an author who fixes
# `status_code = 'X'` to `'C'`, runs `dst apply` (green), and re-asks gets the
# OLD answer — the edit never reached the model. The invariant below: name a
# definition in `definitions` and the compiler injects its CURRENT sql_expr, so an
# applied edit changes the served answer instead of being a no-op behind a green path.


def _crm_model(active_value: str) -> SemanticModel:
    return SemanticModel(
        lens="crm",
        dialect="duckdb",
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="warehouse", table="customers"),
                fields=[
                    Field(name="customer_id", type="integer"),
                    Field(name="status_code", type="string"),
                ],
                metrics=[Metric(name="customer_count", agg="count", expr="customers.customer_id")],
            )
        ],
        definitions=[
            Definition(
                term="active customer",
                body="One whose status_code marks them active.",
                sql_expr=f"customers.status_code = '{active_value}'",
            )
        ],
    )


def test_named_definition_injects_its_current_sql_expr_as_a_filter() -> None:
    intent = QueryIntent(metrics=["customer_count"], definitions=["active customer"])
    assert "customers.status_code = 'C'" in compile_intent(intent, _crm_model("C"))
    # The whole point: editing the definition changes the compiled predicate.
    assert "customers.status_code = 'X'" in compile_intent(intent, _crm_model("X"))


def test_duplicate_predicates_are_merged_once() -> None:
    """The model routinely restates a metric's own filter in `filters`, so the
    same predicate gets ANDed twice. Harmless to results, but a duplicate is a
    merge-path smell — the WHERE dedups on normalized text."""
    intent = QueryIntent(
        metrics=["customer_count"],
        filters=[IntentFilter(field="status_code", op="=", value="C")],
        definitions=["active customer"],
    )
    sql = compile_intent(intent, _crm_model("C"))
    assert sql.lower().count("status_code = 'c'") == 1


def test_an_unknown_or_glossary_definition_falls_the_compile_back() -> None:
    with pytest.raises(CompileError, match="unknown definition 'ghost'"):
        compile_intent(
            QueryIntent(metrics=["customer_count"], definitions=["ghost"]), _crm_model("C")
        )
    glossary = _crm_model("C")
    glossary.definitions[0].sql_expr = None  # explains a term, does not enforce one
    with pytest.raises(CompileError, match="no sql_expr"):
        compile_intent(
            QueryIntent(metrics=["customer_count"], definitions=["active customer"]), glossary
        )


def _seed_crm_warehouse(tmp_path: object) -> DuckDBConnector:
    import duckdb

    path = str(tmp_path / "crm.duckdb")  # type: ignore[operator]
    con = duckdb.connect(path)
    con.execute("CREATE TABLE customers (customer_id INTEGER, status_code VARCHAR)")
    con.execute(
        "INSERT INTO customers SELECT range, 'C' FROM range(44) "
        "UNION ALL SELECT 100 + range, 'X' FROM range(10)"
    )
    con.close()
    return DuckDBConnector(path)


def test_an_applied_definition_edit_changes_the_served_answer(tmp_path: object) -> None:
    """The silent-wrong class, pinned end-to-end through the serving pipeline.

    The model output is IDENTICAL across both serves — it names the definition.
    Only the definition's sql_expr is edited between them, and the served count
    must follow: `'X'` (10 cancelled) -> `'C'` (44 active). If the metric-layer
    prompt does not carry the predicate, both serves return the same wrong
    number and the green apply is a lie.
    """
    connector = _seed_crm_warehouse(tmp_path)
    intent_json = '{"metrics": ["customer_count"], "definitions": ["active customer"]}'

    def _serve(model: SemanticModel) -> int:
        res = run_query(
            question="how many active customers do we have?",
            lens_name="crm",
            org_id="o",
            caller="a",
            semantic_model=model,
            connector=connector,
            generator=IntentSQLGenerator(ScriptedLLM([intent_json])),
            composer=AnswerComposer(ScriptedLLM(["Grounded."])),
        )
        assert res.trace.status == "ok", res.trace
        assert res.response.data is not None
        return int(res.response.data.rows[0][0])

    stale = _serve(_crm_model("X"))
    fixed = _serve(_crm_model("C"))
    assert (stale, fixed) == (10, 44)  # the applied edit reached the served answer
