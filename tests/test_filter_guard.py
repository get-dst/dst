"""Filter guarantee: SQL computing a governed (filtered) metric must carry the
filter — a deterministic check, not a prompt line: the raw-SQL path will
otherwise drop a metric's governed filter and serve the number anyway.

Both directions are load-bearing: violations are caught, and — the harder
contract — a correctly-filtered or non-computing query is NEVER flagged
(false positives block correct answers)."""

from __future__ import annotations

import pytest

from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.compiler import compile_intent
from services.runtime.filter_guard import conflict_note, missing_metric_filters


def _model(dialect: str = "bigquery") -> SemanticModel:
    """A BigQuery sales lens whose `bookings` metric is
    governed by a new-customer filter, plus a second metric with a DIFFERENT
    filter (the mixed-filter compile idiom)."""
    return SemanticModel(
        lens="sales_comp",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name="deals",
                source=EntitySource(connection="bq", table="northwind.crm.deals"),
                fields=[
                    Field(name="segment", type="string"),
                    Field(name="contract_value_eur", type="number"),
                    Field(name="is_new_customer", type="boolean"),
                    Field(name="status", type="string"),
                ],
                metrics=[
                    Metric(
                        name="bookings",
                        agg="sum",
                        expr="deals.contract_value_eur",
                        filters=["deals.is_new_customer = true"],
                    ),
                    Metric(
                        name="won_deals",
                        agg="count",
                        expr="deals.status",
                        filters=["deals.status = 'won'"],
                    ),
                    Metric(name="avg_deal", agg="avg", expr="deals.contract_value_eur"),
                ],
            )
        ],
    )


_UNFILTERED = (
    "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings "
    "FROM northwind.crm.deals AS deals GROUP BY deals.segment"
)


def test_unfiltered_governed_metric_is_reported() -> None:
    # SUM over the metric's expr, GROUP BY segment, no filter.
    assert missing_metric_filters(_UNFILTERED, _model()) == [
        ("bookings", "deals.is_new_customer = true")
    ]


@pytest.mark.parametrize(
    "sql",
    [
        # plain WHERE
        "SELECT deals.segment, SUM(deals.contract_value_eur) AS bookings "
        "FROM northwind.crm.deals AS deals WHERE deals.is_new_customer = true "
        "GROUP BY deals.segment",
        # parenthesized WHERE (the compiler's shared-filter form)
        "SELECT SUM(deals.contract_value_eur) AS bookings "
        "FROM northwind.crm.deals AS deals WHERE (deals.is_new_customer = true)",
        # CASE WHEN wrapping the aggregation (the mixed-filter compile idiom)
        "SELECT SUM(CASE WHEN deals.is_new_customer = true THEN deals.contract_value_eur END) "
        "AS bookings FROM northwind.crm.deals AS deals",
        # bare-boolean WHERE — same meaning, different idiom
        "SELECT SUM(deals.contract_value_eur) AS bookings "
        "FROM northwind.crm.deals AS deals WHERE deals.is_new_customer",
        # BigQuery IF() guard
        "SELECT SUM(IF(deals.is_new_customer, deals.contract_value_eur, NULL)) AS bookings "
        "FROM northwind.crm.deals AS deals",
        # table alias + unqualified columns (single-table scope resolves them)
        "SELECT d.segment, SUM(contract_value_eur) AS bookings "
        "FROM northwind.crm.deals AS d WHERE is_new_customer = TRUE GROUP BY d.segment",
        # filter lives in a CTE feeding the aggregation
        "WITH base AS (SELECT segment, contract_value_eur FROM northwind.crm.deals "
        "WHERE is_new_customer = TRUE) "
        "SELECT segment, SUM(contract_value_eur) AS bookings FROM base GROUP BY segment",
        # filter lives in a subquery feeding the aggregation
        "SELECT SUM(t.contract_value_eur) AS bookings FROM (SELECT contract_value_eur "
        "FROM northwind.crm.deals WHERE is_new_customer = TRUE) AS t",
    ],
)
def test_correctly_filtered_sql_is_never_flagged(sql: str) -> None:
    assert missing_metric_filters(sql, _model()) == []


@pytest.mark.parametrize(
    "sql",
    [
        # no aggregation at all
        "SELECT deals.segment FROM northwind.crm.deals AS deals",
        # a DIFFERENT aggregation over the same column (avg_deal has no filters)
        "SELECT AVG(deals.contract_value_eur) AS avg_deal FROM northwind.crm.deals AS deals",
        # SUM over a different column
        "SELECT SUM(1) AS n FROM northwind.crm.deals AS deals",
    ],
)
def test_query_not_computing_the_metric_is_never_flagged(sql: str) -> None:
    assert missing_metric_filters(sql, _model()) == []


def test_multi_metric_mixed_filters_each_case_guarded_passes() -> None:
    sql = (
        "SELECT SUM(CASE WHEN (deals.is_new_customer = true) THEN deals.contract_value_eur END) "
        "AS bookings, COUNT(CASE WHEN (deals.status = 'won') THEN deals.status END) AS won_deals "
        "FROM northwind.crm.deals AS deals"
    )
    assert missing_metric_filters(sql, _model()) == []


def test_multi_metric_reports_only_the_unfiltered_one() -> None:
    sql = (
        "SELECT SUM(deals.contract_value_eur) AS bookings, "
        "COUNT(CASE WHEN deals.status = 'won' THEN deals.status END) AS won_deals "
        "FROM northwind.crm.deals AS deals"
    )
    assert missing_metric_filters(sql, _model()) == [("bookings", "deals.is_new_customer = true")]


def test_shared_where_does_not_satisfy_a_second_metrics_own_filter() -> None:
    # bookings' filter in WHERE, won_deals computed with NO status filter anywhere.
    sql = (
        "SELECT SUM(deals.contract_value_eur) AS bookings, COUNT(deals.status) AS won_deals "
        "FROM northwind.crm.deals AS deals WHERE deals.is_new_customer = true"
    )
    assert missing_metric_filters(sql, _model()) == [("won_deals", "deals.status = 'won'")]


@pytest.mark.parametrize("dimensions", [[], ["segment"]])
def test_intent_path_outputs_always_pass(dimensions: list[str]) -> None:
    """The compiler self-contains metric filters (shared WHERE, or CASE per
    aggregation when filter sets differ) — its output must sail through."""
    model = _model()
    shared = compile_intent(QueryIntent(metrics=["bookings"], dimensions=dimensions), model)
    assert missing_metric_filters(shared, model) == []
    mixed = compile_intent(
        QueryIntent(metrics=["bookings", "won_deals"], dimensions=dimensions), model
    )
    assert missing_metric_filters(mixed, model) == []


def test_single_element_in_satisfies_an_equality_filter() -> None:
    """A filter authored ``= 'won'`` but generated as ``IN ('won')`` is the same
    condition — never flagged (false positives block correct answers)."""
    sql = (
        "SELECT COUNT(deals.status) AS won_deals FROM northwind.crm.deals AS deals "
        "WHERE deals.status IN ('won')"
    )
    assert missing_metric_filters(sql, _model()) == []


def test_duckdb_dialect_and_count_metric() -> None:
    model = _model("duckdb")
    flagged = missing_metric_filters(
        "SELECT COUNT(deals.status) AS won_deals FROM northwind.crm.deals AS deals", model
    )
    assert flagged == [("won_deals", "deals.status = 'won'")]
    ok = missing_metric_filters(
        "SELECT COUNT(deals.status) AS won_deals FROM northwind.crm.deals AS deals "
        "WHERE deals.status = 'won'",
        model,
    )
    assert ok == []


def test_joined_foreign_column_sum_is_not_mistaken_for_the_metric() -> None:
    """A SUM over ANOTHER table's same-named column must not bind to the metric —
    strict attribution, or a correct query gets blocked."""
    model = _model()
    model.entities.append(
        Entity(
            name="renewals",
            source=EntitySource(connection="bq", table="northwind.crm.renewals"),
            fields=[
                Field(name="deal_id", type="integer"),
                Field(name="contract_value_eur", type="number"),
            ],
        )
    )
    sql = (
        "SELECT SUM(r.contract_value_eur) AS renewal_value FROM northwind.crm.renewals AS r "
        "JOIN northwind.crm.deals AS d ON r.deal_id = d.segment"
    )
    assert missing_metric_filters(sql, model) == []
    # And in multi-table scope an UNQUALIFIED column is ambiguous → never flagged.
    ambiguous = (
        "SELECT SUM(contract_value_eur) AS v FROM northwind.crm.renewals AS r "
        "JOIN northwind.crm.deals AS d ON r.deal_id = d.segment"
    )
    assert missing_metric_filters(ambiguous, model) == []


def test_unparseable_sql_is_not_this_modules_problem() -> None:
    assert missing_metric_filters("SELECT WHERE FROM (((", _model()) == []


# ── attribution by resolution + the twin rule ────────────────────────────────


def _twin_model(*, with_unfiltered_twin: bool = True) -> SemanticModel:
    """The six-act H2 shape: session_count and converted_sessions share
    COUNT(sessions.session_id) — only the latter is governed by a filter."""
    metrics = [
        Metric(
            name="converted_sessions",
            agg="count",
            expr="sessions.session_id",
            filters=["sessions.converted = true"],
        ),
    ]
    if with_unfiltered_twin:
        metrics.insert(0, Metric(name="session_count", agg="count", expr="sessions.session_id"))
    return SemanticModel(
        lens="probe",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="web_sessions"),
                fields=[
                    Field(name="session_id", type="string"),
                    Field(name="converted", type="boolean"),
                ],
                metrics=metrics,
            )
        ],
    )


_BARE_COUNT = "SELECT COUNT(sessions.session_id) AS n FROM web_sessions AS sessions"


def test_h2_unfiltered_twin_legitimizes_the_bare_aggregation() -> None:
    """'how many sessions' is session_count, not an unfiltered converted_sessions —
    a bare aggregation violates only when EVERY matching metric requires filters."""
    assert missing_metric_filters(_BARE_COUNT, _twin_model()) == []


def test_h2_only_the_filtered_metric_still_flags() -> None:
    assert missing_metric_filters(_BARE_COUNT, _twin_model(with_unfiltered_twin=False)) == [
        ("converted_sessions", "sessions.converted = true")
    ]


def test_h2_guarded_filtered_twin_still_passes() -> None:
    sql = (
        "SELECT COUNT(CASE WHEN sessions.converted = true THEN sessions.session_id END) AS n "
        "FROM web_sessions AS sessions"
    )
    assert missing_metric_filters(sql, _twin_model(with_unfiltered_twin=False)) == []


def test_two_filtered_twins_flag_unless_one_is_satisfied() -> None:
    """With ONLY filtered twins, a bare form flags them all (which one it meant is
    unknowable); satisfying either twin's filter clears the form for the group."""
    model = _twin_model(with_unfiltered_twin=False)
    model.entities[0].fields.append(Field(name="device", type="string"))
    model.entities[0].metrics.append(
        Metric(
            name="mobile_sessions",
            agg="count",
            expr="sessions.session_id",
            filters=["sessions.device = 'mobile'"],
        )
    )
    assert missing_metric_filters(_BARE_COUNT, model) == [
        ("converted_sessions", "sessions.converted = true"),
        ("mobile_sessions", "sessions.device = 'mobile'"),
    ]
    satisfied = _BARE_COUNT + " WHERE sessions.device = 'mobile'"
    assert missing_metric_filters(satisfied, model) == []


def test_distinct_twin_is_a_different_shape() -> None:
    # COUNT(x) and COUNT(DISTINCT x) are different aggregations: an unfiltered
    # DISTINCT twin must not legitimize the bare plain COUNT.
    model = _twin_model(with_unfiltered_twin=False)
    model.entities[0].metrics.append(
        Metric(name="unique_sessions", agg="count_distinct", expr="sessions.session_id")
    )
    assert missing_metric_filters(_BARE_COUNT, model) == [
        ("converted_sessions", "sessions.converted = true")
    ]


# ── bound implication ────────────────────────────────────────────────────────


def _bounded_model() -> SemanticModel:
    """A metric whose governed filter is an inequality BOUND — the shape a
    naive check rejects for every narrower window ('yesterday' fails
    `<= CURRENT_DATE`)."""
    return SemanticModel(
        lens="rev",
        dialect="bigquery",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="bq", table="analytics.orders_daily"),
                fields=[
                    Field(name="amount", type="number"),
                    Field(name="ordered_at", type="date"),
                ],
                metrics=[
                    Metric(
                        name="revenue",
                        agg="sum",
                        expr="orders.amount",
                        filters=["orders.ordered_at <= CURRENT_DATE"],
                    )
                ],
            )
        ],
    )


def test_a_strictly_narrower_predicate_satisfies_a_metric_bound() -> None:
    # Yesterday cannot be after today: `=` DATE_SUB(CURRENT_DATE, 1 DAY) entails
    # `<= CURRENT_DATE`, and the guard used to reject it as unfiltered.
    sql = (
        "SELECT SUM(orders.amount) AS revenue FROM analytics.orders_daily AS orders "
        "WHERE orders.ordered_at = DATE_SUB(CURRENT_DATE, INTERVAL '1' DAY)"
    )
    assert missing_metric_filters(sql, _bounded_model()) == []


def test_an_equality_against_a_past_literal_satisfies_the_bound() -> None:
    sql = (
        "SELECT SUM(orders.amount) AS revenue FROM analytics.orders_daily AS orders "
        "WHERE orders.ordered_at = DATE '2026-01-01'"
    )
    assert missing_metric_filters(sql, _bounded_model()) == []


def test_a_range_inside_the_bound_satisfies_it() -> None:
    sql = (
        "SELECT SUM(orders.amount) AS revenue FROM analytics.orders_daily AS orders "
        "WHERE orders.ordered_at BETWEEN DATE '2026-01-01' AND DATE '2026-01-31'"
    )
    assert missing_metric_filters(sql, _bounded_model()) == []


def test_a_predicate_that_reaches_past_the_bound_is_still_caught() -> None:
    # `>=` admits FUTURE rows the metric excludes — must still flag.
    sql = (
        "SELECT SUM(orders.amount) AS revenue FROM analytics.orders_daily AS orders "
        "WHERE orders.ordered_at >= DATE '2026-01-01'"
    )
    assert missing_metric_filters(sql, _bounded_model()) != []


def test_no_predicate_at_all_is_still_caught() -> None:
    sql = "SELECT SUM(orders.amount) AS revenue FROM analytics.orders_daily AS orders"
    assert missing_metric_filters(sql, _bounded_model()) != []


def test_an_equality_filter_requirement_still_demands_equality() -> None:
    # An equality-style governed filter has no "narrower" — text/structural
    # matching stays, and the WRONG value must still flag.
    sql = (
        "SELECT SUM(deals.contract_value_eur) AS bookings FROM northwind.crm.deals AS deals "
        "WHERE deals.is_new_customer = false"
    )
    assert missing_metric_filters(sql, _model()) != []


# ─── incompatible twins resolve by the question, never the union ─────────────


def _snapshot_model() -> SemanticModel:
    """The standard snapshot-table pattern that deadlocks the guard:
    current_arr pins the latest snapshot date, total_arr pins end-of-month —
    same expression, jointly unsatisfiable filters on ~29 days in 30."""
    return SemanticModel(
        lens="finance",
        dialect="bigquery",
        entities=[
            Entity(
                name="finance_kpis",
                source=EntitySource(connection="bq", table="proj.marts.finance_kpis"),
                fields=[
                    Field(name="arr_usd", type="number"),
                    Field(name="status_date", type="date"),
                    Field(name="is_end_of_month", type="boolean"),
                    Field(name="tier", type="string"),
                ],
                metrics=[
                    Metric(
                        name="current_arr",
                        agg="sum",
                        expr="finance_kpis.arr_usd",
                        filters=[
                            "finance_kpis.status_date = (SELECT MAX(finance_kpis.status_date) "
                            "FROM proj.marts.finance_kpis AS finance_kpis)"
                        ],
                    ),
                    Metric(
                        name="total_arr",
                        agg="sum",
                        expr="finance_kpis.arr_usd",
                        filters=["finance_kpis.is_end_of_month = TRUE"],
                    ),
                ],
            )
        ],
    )


_BARE_ARR = (
    "SELECT finance_kpis.tier, SUM(finance_kpis.arr_usd) AS arr "
    "FROM proj.marts.finance_kpis AS finance_kpis GROUP BY finance_kpis.tier"
)


def test_the_question_resolves_incompatible_twins_to_one_metric() -> None:
    """'current ARR by tier' names current_arr — only ITS filter is demanded,
    so the repair loop has a satisfiable target instead of an impossible union."""
    missing = missing_metric_filters(
        _BARE_ARR, _snapshot_model(), question="Break our current ARR down by customer tier"
    )
    assert missing, "an unfiltered governed aggregation must still be caught"
    assert {name for name, _f in missing} == {"current_arr"}


def test_a_question_naming_neither_twin_keeps_the_union_and_names_the_conflict() -> None:
    missing = missing_metric_filters(
        _BARE_ARR, _snapshot_model(), question="Break our ARR down by customer tier"
    )
    assert {name for name, _f in missing} == {"current_arr", "total_arr"}
    note = conflict_note(missing, _snapshot_model())
    assert note is not None
    assert "current_arr" in note and "total_arr" in note
    assert "naming the metric" in note or "re-ask" in note


def test_a_query_satisfying_one_twin_still_clears_without_a_question() -> None:
    sql = (
        "SELECT finance_kpis.tier, SUM(finance_kpis.arr_usd) AS arr "
        "FROM proj.marts.finance_kpis AS finance_kpis "
        "WHERE finance_kpis.is_end_of_month = TRUE GROUP BY finance_kpis.tier"
    )
    assert missing_metric_filters(sql, _snapshot_model()) == []


def test_conflict_note_is_none_for_a_single_metric_miss() -> None:
    missing = [("current_arr", "whatever")]
    assert conflict_note(missing, _snapshot_model()) is None


# ── the latest-snapshot predicate, however it is spelled ─────────────────────
# Both generation prompts steer 'now'/'current' on a status-date column to
# `col = (SELECT MAX(col) FROM …)`. A metric governed by exactly that filter was
# then rejected as UNFILTERED whenever the model aliased the inner table or
# hoisted the MAX into a CTE — a correct query refused, which is the one failure
# mode this module must not have.

_LATEST = "usage.status_date = (SELECT MAX(status_date) FROM proj.marts.usage_daily)"


def _snapshot_filter_model() -> SemanticModel:
    return SemanticModel(
        lens="product_usage",
        dialect="bigquery",
        entities=[
            Entity(
                name="usage",
                source=EntitySource(connection="bq", table="proj.marts.usage_daily"),
                fields=[
                    Field(name="account_id", type="string"),
                    Field(name="is_engaged", type="boolean"),
                    Field(name="status_date", type="date"),
                ],
                metrics=[
                    Metric(
                        name="engaged_accounts",
                        agg="count_distinct",
                        expr="usage.account_id",
                        filters=["usage.is_engaged = TRUE", _LATEST],
                    )
                ],
            )
        ],
    )


_HEAD = (
    "SELECT COUNT(DISTINCT usage.account_id) AS engaged_accounts "
    "FROM proj.marts.usage_daily AS usage "
)


@pytest.mark.parametrize(
    ("label", "predicate"),
    [
        ("verbatim", f"AND {_LATEST}"),
        (
            "inner table aliased",
            "AND usage.status_date = (SELECT MAX(u2.status_date) "
            "FROM proj.marts.usage_daily AS u2)",
        ),
    ],
)
def test_the_governed_latest_snapshot_predicate_is_credited_in_any_spelling(
    label: str, predicate: str
) -> None:
    sql = f"{_HEAD}WHERE usage.is_engaged = TRUE {predicate}"
    assert missing_metric_filters(sql, _snapshot_filter_model()) == [], label


def test_the_max_hoisted_into_a_cte_is_the_same_predicate() -> None:
    sql = (
        "WITH latest AS (SELECT MAX(status_date) AS max_date FROM proj.marts.usage_daily) "
        f"{_HEAD}WHERE usage.is_engaged = TRUE "
        "AND usage.status_date = (SELECT max_date FROM latest)"
    )
    assert missing_metric_filters(sql, _snapshot_filter_model()) == []


@pytest.mark.parametrize(
    ("label", "predicate"),
    [
        ("today's literal is not the latest available date", "usage.status_date = CURRENT_DATE"),
        (
            "a max over a DIFFERENT table is a different value",
            "usage.status_date = (SELECT MAX(status_date) FROM proj.marts.other_daily)",
        ),
        (
            "a NARROWED max is a different value",
            "usage.status_date = (SELECT MAX(status_date) FROM proj.marts.usage_daily "
            "WHERE region = 'EU')",
        ),
        (
            "a max over a different COLUMN is a different value",
            "usage.status_date = (SELECT MAX(ingested_at) FROM proj.marts.usage_daily)",
        ),
    ],
)
def test_only_the_declared_snapshot_predicate_clears_the_filter(label: str, predicate: str) -> None:
    sql = f"{_HEAD}WHERE usage.is_engaged = TRUE AND {predicate}"
    missing = missing_metric_filters(sql, _snapshot_filter_model())
    assert [f for _n, f in missing] == [_LATEST], label


def test_a_metric_with_two_governed_filters_is_named_once() -> None:
    """The rejection sentence is per METRIC, not per fragment: two clauses
    naming one metric read as two metrics in conflict."""
    from services.runtime.pipeline import _governed_metric_clauses

    unfiltered = [("engaged_accounts", "usage.is_engaged = TRUE"), ("engaged_accounts", _LATEST)]
    clauses = _governed_metric_clauses(unfiltered)
    assert clauses.count("engaged_accounts") == 1
    assert "usage.is_engaged = TRUE AND " + _LATEST in clauses
