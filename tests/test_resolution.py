"""The resolution ledger: every slot of an answer's meaning names its source.

Pinned here: the intent door and the attribution door agree on the same SQL
(one vocabulary, two readers); a column-name collision across entities never
attributes (the marketing_spend incident); a sibling-metric tie without a SELECT
alias is reported as the aggregate the SQL wrote, never a guessed sibling; a
filter literal is declared only inside a COMPLETE dictionary; a definition's
predicate in WHERE is the definition, not an invented filter; unparseable SQL is
``unknown`` out loud; and the tag table.
"""

from __future__ import annotations

import pytest

from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.resolution import Slot, derive_tag
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.resolution import attribute, certified_overlay, from_intent, grade


def _model() -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="orders"),
                default_time_field="order_date",
                fields=[
                    Field(name="amount", type="number"),
                    Field(name="status", type="string"),
                    Field(name="country", type="string"),
                    Field(name="order_date", type="date"),
                ],
                dimensions=[Dimension(name="country")],
                metrics=[
                    Metric(name="revenue", agg="sum", expr="orders.amount"),
                    Metric(
                        name="shipped_revenue",
                        agg="sum",
                        expr="orders.amount",
                        filters=["orders.status = 'shipped'"],
                    ),
                    Metric(name="order_count", agg="count"),
                ],
            )
        ],
        definitions=[
            Definition(
                term="large_order",
                body="an order over 1000",
                sql_expr="orders.amount > 1000",
            )
        ],
    )


DOMAINS = {"country": ["FI", "DK"]}


def _slots(res, kind: str) -> list[tuple[str, str]]:
    return [(s.name, s.source) for s in res.slots if s.kind == kind]


# ── the two doors agree ──────────────────────────────────────────────────────


def test_intent_and_attribution_agree_on_compiled_sql() -> None:
    intent = QueryIntent(
        entity="orders",
        metrics=["revenue"],
        dimensions=["country"],
        grain="month",
        filters=[IntentFilter(field="country", op="=", value="FI")],
    )
    model = _model()
    built = from_intent(intent, model, DOMAINS)
    read = attribute(compile_intent(intent, model), model, DOMAINS)
    assert built.method == "construction" and read.method == "attributed"
    for kind in ("metric", "dimension", "grain", "filter"):
        assert _slots(built, kind) == _slots(read, kind), kind
    assert built.tag == read.tag == "declared"
    grain = next(s for s in read.slots if s.kind == "grain")
    assert (grain.name, grain.value) == ("order_date", "month")


def test_intent_filters_that_restate_governed_predicates_are_not_inferred() -> None:
    """Seen on a field corpus: the intent tier restated the entity's
    population filter (`is_current = true`) as an explicit filter and the
    ledger called it inferred; and a boolean literal on a boolean field has a
    closed domain by type."""
    model = SemanticModel(
        lens="crm",
        dialect="duckdb",
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="wh", table="marts.dim_customer"),
                population="current customers only",
                population_filter="is_current = true",
                fields=[
                    Field(name="customer_id", type="string"),
                    Field(name="is_current", type="boolean"),
                    Field(name="is_key_account", type="boolean"),
                    Field(name="primary_segment", type="string"),
                ],
                dimensions=[Dimension(name="primary_segment")],
                metrics=[
                    Metric(
                        name="customer_count", agg="count_distinct", expr="customers.customer_id"
                    )
                ],
            )
        ],
    )
    intent = QueryIntent(
        entity="customers",
        metrics=["customer_count"],
        dimensions=["primary_segment"],
        filters=[
            IntentFilter(field="is_current", op="=", value=True),
            IntentFilter(field="is_key_account", op="=", value=True),
        ],
    )
    res = from_intent(intent, model, {})
    assert _slots(res, "filter") == [("is_key_account", "declared")]
    assert res.tag == "declared"
    # The same restated predicate read back from compiled SQL is dropped too.
    read = attribute(compile_intent(intent, model), model, {})
    assert _slots(read, "filter") == [("is_key_account", "declared")]


# ── attribution edges ────────────────────────────────────────────────────────


def test_bare_aggregate_is_inferred_and_tag_says_so() -> None:
    res = attribute("SELECT country, SUM(amount * 2) AS x FROM orders GROUP BY 1", _model())
    assert _slots(res, "metric") == [("SUM(amount * 2)", "inferred")]
    assert _slots(res, "dimension") == [("country", "declared")]
    assert res.tag == "inferred"


def test_sibling_metric_tie_needs_an_alias() -> None:
    """revenue and shipped_revenue compile to the same bare SUM; without an alias
    the ledger names the aggregate, never one of the siblings."""
    model = _model()
    tied = attribute("SELECT SUM(orders.amount) FROM orders", model)
    assert _slots(tied, "metric") == [("SUM(orders.amount)", "inferred")]
    aliased = attribute("SELECT SUM(orders.amount) AS shipped_revenue FROM orders", model)
    assert _slots(aliased, "metric") == [("shipped_revenue", "declared")]
    assert aliased.tag == "declared"


def test_column_name_collision_across_entities_never_attributes() -> None:
    """The marketing_spend incident: `amount` on three tables, SQL touching one."""
    model = SemanticModel(
        lens="ops",
        dialect="duckdb",
        entities=[
            Entity(
                name=name,
                source=EntitySource(connection="wh", table=f"ops.{name}"),
                fields=[Field(name="amount", type="number")],
                metrics=[Metric(name=f"total_{name}", agg="sum", expr=f"{name}.amount")],
            )
            for name in ("payments", "marketing_spend")
        ],
    )
    res = attribute("SELECT SUM(p.amount) AS total_payments FROM ops.payments AS p", model)
    assert _slots(res, "metric") == [("total_payments", "declared")]


def test_filter_literal_declared_only_inside_a_complete_domain() -> None:
    model = _model()
    sql = "SELECT SUM(orders.amount) AS revenue FROM orders WHERE country = 'FI'"
    assert _slots(attribute(sql, model, DOMAINS), "filter") == [("country", "declared")]
    assert _slots(attribute(sql, model, {}), "filter") == [("country", "inferred")]
    outside = sql.replace("'FI'", "'Finland'")
    assert _slots(attribute(outside, model, DOMAINS), "filter") == [("country", "inferred")]
    # Same rule on the construction door.
    intent = QueryIntent(
        entity="orders",
        metrics=["revenue"],
        filters=[IntentFilter(field="country", op="=", value="Finland")],
    )
    assert _slots(from_intent(intent, model, DOMAINS), "filter") == [("country", "inferred")]


def test_definition_predicate_is_the_definition_not_a_filter() -> None:
    sql = (
        "SELECT COUNT(*) AS order_count FROM orders "
        "WHERE orders.amount > 1000 AND orders.order_date >= '2026-01-01'"
    )
    res = attribute(sql, _model())
    assert _slots(res, "definition") == [("large_order", "declared")]
    assert _slots(res, "filter") == []
    assert _slots(res, "window") == [("order_date", "inferred")]
    assert res.tag == "declared"


def test_listing_answer_has_no_measure_and_is_inferred() -> None:
    res = attribute("SELECT * FROM orders LIMIT 5", _model())
    assert _slots(res, "metric") == []
    assert res.tag == "inferred"


def test_unparseable_sql_is_unknown_never_guessed() -> None:
    res = attribute("SELECT FROM WHERE (", _model())
    assert res.tag == "unknown"
    assert [s.source for s in res.slots] == ["unknown"]
    assert attribute("", _model()).tag == "unknown"


def test_grain_over_an_undeclared_time_column_is_inferred() -> None:
    sql = "SELECT DATE_TRUNC('week', shipped_at) AS w, COUNT(*) FROM orders GROUP BY 1"
    res = attribute(sql, _model())
    grain = next(s for s in res.slots if s.kind == "grain")
    assert (grain.name, grain.source, grain.value) == ("shipped_at", "inferred", "week")


# ── an inline ratio is the ratio ─────────────────────────────────────────────


def _ratio_model() -> SemanticModel:
    return SemanticModel(
        lens="matches",
        dialect="duckdb",
        entities=[
            Entity(
                name="pub_matches",
                source=EntitySource(connection="wh", table="marts.fact_pub_match"),
                fields=[
                    Field(name="radiant_win", type="boolean"),
                    Field(name="game_type", type="string"),
                    Field(name="duration", type="number"),
                    Field(name="patch_id", type="number"),
                ],
                metrics=[
                    Metric(
                        name="radiant_wins",
                        agg="sum",
                        expr="CASE WHEN pub_matches.radiant_win THEN 1 ELSE 0 END",
                    ),
                    Metric(name="match_count", agg="count"),
                    Metric(
                        name="radiant_win_rate",
                        type="ratio",
                        numerator="radiant_wins",
                        denominator="match_count",
                    ),
                ],
            ),
            Entity(
                name="patches",
                source=EntitySource(connection="wh", table="marts.dim_patch"),
                fields=[
                    Field(name="patch_id", type="number"),
                    Field(name="is_current", type="boolean"),
                ],
            ),
        ],
    )


_FROM = (
    " FROM marts.fact_pub_match AS pub_matches JOIN marts.dim_patch AS patches"
    " ON patches.patch_id = pub_matches.patch_id"
    " WHERE pub_matches.game_type = 'ranked_all_pick' AND patches.is_current = TRUE"
)
_WINS = "SUM(CASE WHEN radiant_win THEN 1 ELSE 0 END)"


def _metrics(sql: str) -> list[tuple[str, str]]:
    return _slots(attribute(sql, _ratio_model()), "metric")


def _pk_count_model() -> SemanticModel:
    """The ratio model with match_count declared the way real projects write it:
    COUNT of the primary key column, not a bare COUNT(*)."""
    model = _ratio_model()
    matches = model.entities[0]
    matches.primary_key = ["match_id"]
    matches.fields.append(Field(name="match_id", type="number"))
    matches.metrics[1] = Metric(name="match_count", agg="count", expr="pub_matches.match_id")
    return model


@pytest.mark.parametrize(
    "select",
    [
        f"{_WINS} * 1.0 / COUNT(*)",
        f"{_WINS} * 1.0 / COUNT(pub_matches.match_id)",
        f"{_WINS} * 1.0 / COUNT(1)",
    ],
)
def test_a_count_of_the_primary_key_is_a_count_of_every_row(select: str) -> None:
    """COUNT(*), COUNT(1) and COUNT(<primary key>) are one count: a key is never
    null. A ratio whose denominator counts the key reads back as the ratio."""
    got = _slots(attribute(f"SELECT {select}{_FROM}", _pk_count_model()), "metric")
    assert got == [("radiant_win_rate", "declared")]


def test_a_count_of_a_nullable_column_is_not_every_row() -> None:
    got = _slots(
        attribute(f"SELECT {_WINS} * 1.0 / COUNT(duration){_FROM}", _pk_count_model()), "metric"
    )
    assert ("radiant_win_rate", "declared") not in got


@pytest.mark.parametrize(
    "select",
    [
        f"{_WINS} * 1.0 / COUNT(*) AS radiant_win_rate",
        f"CAST({_WINS} AS DOUBLE) / NULLIF(COUNT(*), 0)",
        f"{_WINS} / COUNT(*)",
        f"1.0 * {_WINS} / COUNT(1)",
    ],
)
def test_inline_ratio_attributes_the_ratio_not_its_operands(select: str) -> None:
    assert _metrics(f"SELECT {select}{_FROM}") == [("radiant_win_rate", "declared")]


def test_inline_ratio_grades_equal_to_the_typed_form() -> None:
    model = _ratio_model()
    intent = QueryIntent(entity="pub_matches", metrics=["radiant_win_rate"])
    compiled = compile_intent(intent, model)
    certified = attribute(f"SELECT {_WINS} * 1.0 / COUNT(*) AS radiant_win_rate{_FROM}", model)
    assert _slots(attribute(compiled, model), "metric") == _slots(certified, "metric")
    assert grade(certified, from_intent(intent, model, None))[0] == "passed"


def test_bare_numerator_still_attributes_the_simple_metric() -> None:
    assert _metrics(f"SELECT {_WINS} AS radiant_wins{_FROM}") == [("radiant_wins", "declared")]


def test_division_of_unrelated_aggregations_is_no_ratio() -> None:
    for select in (f"{_WINS} / SUM(duration)", "SUM(duration) / COUNT(*)"):
        names = [n for n, _s in _metrics(f"SELECT {select}{_FROM}")]
        assert "radiant_win_rate" not in names


# ── overlay + tag table ──────────────────────────────────────────────────────


def test_certified_overlay_keeps_names_and_certifies_every_slot() -> None:
    res = attribute("SELECT SUM(amount * 2) AS x FROM orders WHERE country = 'FI'", _model())
    cert = certified_overlay(res)
    assert cert.tag == "certified"
    assert {s.source for s in cert.slots} == {"certified"}
    assert [s.name for s in cert.slots] == [s.name for s in res.slots]


@pytest.mark.parametrize(
    ("slots", "certification", "tag"),
    [
        ([], "certified", "certified"),
        ([("metric", "declared")], "none", "declared"),
        ([("metric", "declared"), ("definition", "declared")], "none", "declared"),
        ([("metric", "declared"), ("metric", "inferred")], "none", "mixed"),
        ([("metric", "inferred")], "none", "inferred"),
        ([], "none", "inferred"),
        ([("metric", "declared"), ("filter", "inferred")], "none", "declared"),
        ([("metric", "declared"), ("window", "inferred")], "none", "declared"),
        ([("metric", "unknown")], "none", "unknown"),
    ],
)
def test_tag_table(slots: list[tuple[str, str]], certification: str, tag: str) -> None:
    built = [Slot(kind=k, name="x", source=s) for k, s in slots]  # type: ignore[arg-type]
    assert derive_tag(built, certification) == tag


def test_certified_overlay_is_typed_by_approval_and_keeps_its_decisions() -> None:
    from services.contracts.resolution import DecisionRecord

    res = attribute("SELECT SUM(amount * 2) AS x FROM orders", _model())
    res.decisions = [
        DecisionRecord(
            slot="certified_equivalent", chosen="equivalent", verdict="act", provider="v"
        )
    ]
    cert = certified_overlay(res)
    assert cert.typed is True and cert.decisions == res.decisions
