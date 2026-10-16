"""level_grain, and the false partial reasons on approved and counted SQL.

Three checks capped serves at `partial` for reasons the SQL did not earn:
window_applied on certified snapshot SQL whose approved question states the
asked month; aggregation_scope on a COUNT (a row count is in no currency)
whose only SUM/MAX was the latest-snapshot pin; metric_filter_waived on a
bare COUNT(*) attributed to every filtered count metric. And one silent-wrong
shape no check saw: a level metric SUMmed per month over its daily snapshots.
"""

from __future__ import annotations

from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.compiler import compile_intent
from services.runtime.filter_guard import metric_filter_findings
from services.runtime.generator import serialize_model
from services.runtime.verification import (
    _aggregation_scope,
    _level_grain,
    _window_applied,
    build_report,
)


def _balances(*, level: bool = True, grain: str | None = None) -> SemanticModel:
    """Daily account balances in EUR, pinned on currency."""
    return SemanticModel(
        lens="treasury",
        dialect="duckdb",
        entities=[
            Entity(
                name="balances",
                source=EntitySource(connection="wh", table="fin.balances"),
                default_time_field="day",
                grain=grain,
                pinned_dimensions=["currency"],
                fields=[
                    Field(name="day", type="date"),
                    Field(name="account_id", type="string"),
                    Field(name="balance", type="number"),
                    Field(name="currency", type="string"),
                ],
                metrics=[
                    Metric(
                        name="balance",
                        agg="sum",
                        expr="balances.balance",
                        currency="EUR",
                        additivity="level" if level else None,
                    ),
                    Metric(
                        name="overdrawn_accounts",
                        agg="count",
                        filters=["balances.balance < 0"],
                    ),
                ],
            )
        ],
    )


_PIN = "balances.day = (SELECT MAX(balances.day) FROM fin.balances AS balances)"
_COUNT_PINNED = f"SELECT COUNT(*) AS n FROM fin.balances AS balances WHERE {_PIN}"
# A current-state table: the approved SQL carries no date handling at all.
_COUNT_SNAPSHOT = "SELECT COUNT(*) AS n FROM fin.balances AS balances"


def _report(question: str, sql: str, **kw: object):  # noqa: ANN202
    return build_report(
        question=question,
        sql=sql,
        answer_text="",
        result=QueryResult(columns=["n"], rows=[[1]]),
        semantic_model=_balances(),
        definition_used=None,
        truncated=False,
        composed=False,
        uncomposed_reason="rows-only test",
        **kw,  # type: ignore[arg-type]
    )


# ── window_applied ───────────────────────────────────────────────────────────


def test_certified_snapshot_sql_for_the_approved_month_passes_the_window_check() -> None:
    report = _report(
        "How many accounts did we hold in September 2026?",
        _COUNT_SNAPSHOT,
        certification="certified",
        certified_question="Number of accounts held in September 2026",
    )
    check = report.check("window_applied")
    assert check is not None and check.status == "pass"
    assert report.grade == "verified"
    assert report.partial_reason is None


def test_a_certified_serve_matched_onto_another_window_keeps_the_deterministic_read() -> None:
    check = _window_applied(
        "How many accounts did we hold in September 2026?",
        _COUNT_SNAPSHOT,
        _balances(),
        certification="certified",
        certified_question="Number of accounts held in August 2026",
    )
    assert check.status == "fail"
    assert _window_applied(
        "How many accounts did we hold in September 2026?", _COUNT_SNAPSHOT, _balances()
    ).status == ("fail")


def test_a_level_metric_pinned_to_the_latest_snapshot_is_this_months_figure() -> None:
    sql = f"SELECT SUM(balances.balance) AS b FROM fin.balances AS balances WHERE {_PIN}"
    assert _window_applied("What is our total balance this month?", sql, _balances()).status == (
        "pass"
    )
    # a past period is not the latest snapshot, level or not
    assert _window_applied("What was our total balance last month?", sql, _balances()).status == (
        "fail"
    )
    # a flow summed from one snapshot day is still one day, not the period
    assert _window_applied(
        "What is our total balance this month?", sql, _balances(level=False)
    ).status == ("fail")


# ── aggregation_scope ────────────────────────────────────────────────────────


def test_a_count_never_complains_about_currency() -> None:
    check = _aggregation_scope(_COUNT_PINNED, _balances())
    assert check.status == "skip"
    assert "currency" not in (check.reason or "") or "nothing denominated" in (check.reason or "")


def test_a_sum_over_a_column_whose_metric_declares_its_currency_passes() -> None:
    sql = "SELECT SUM(balances.balance) AS b FROM fin.balances AS balances"
    assert _aggregation_scope(sql, _balances()).status == "pass"


def test_a_sum_over_an_undeclared_column_still_fails_scope() -> None:
    model = _balances()
    model.entities[0].fields.append(Field(name="credit_line", type="number"))
    sql = "SELECT SUM(balances.credit_line) AS c FROM fin.balances AS balances"
    check = _aggregation_scope(sql, model)
    assert check.status == "fail"
    assert "denominated in nothing" in (check.reason or "")


# ── metric_filter_waived ─────────────────────────────────────────────────────


def test_a_bare_row_count_is_not_every_count_metric() -> None:
    findings = metric_filter_findings(
        _COUNT_PINNED, _balances(), question="How many rows do we hold right now?"
    )
    assert findings.missing == []
    assert findings.waived == []


def test_a_served_filter_is_never_waived() -> None:
    sql = "SELECT COUNT(*) AS n FROM fin.balances AS balances WHERE balances.balance < 0"
    findings = metric_filter_findings(sql, _balances(), question="How many are overdrawn?")
    assert findings.missing == [] and findings.waived == []


def test_a_row_count_the_question_names_still_demands_the_filter() -> None:
    findings = metric_filter_findings(
        "SELECT COUNT(*) AS n FROM fin.balances AS balances",
        _balances(),
        question="How many overdrawn accounts do we have?",
    )
    assert findings.missing == [("overdrawn_accounts", "balances.balance < 0")]


def test_an_expression_count_missing_its_filter_is_still_waived() -> None:
    model = SemanticModel(
        lens="finance",
        dialect="duckdb",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection="wh", table="finance.invoices"),
                fields=[Field(name="invoice_id", type="number")],
                metrics=[
                    Metric(
                        name="overdue_invoice_count",
                        agg="count",
                        expr="invoices.invoice_id",
                        filters=["invoices.days_overdue > 0"],
                    )
                ],
            )
        ],
    )
    findings = metric_filter_findings(
        "SELECT COUNT(invoices.invoice_id) AS n FROM finance.invoices AS invoices",
        model,
        question="How many invoices in total?",
    )
    assert findings.waived == [("overdue_invoice_count", "invoices.days_overdue > 0")]


# ── level_grain ──────────────────────────────────────────────────────────────

_SUMMED_BY_MONTH = (
    "SELECT DATE_TRUNC('month', balances.day) AS month, SUM(balances.balance) AS balance "
    "FROM fin.balances AS balances GROUP BY 1"
)


def test_a_level_metric_summed_per_month_fails() -> None:
    report = _report("Balance by month this year", _SUMMED_BY_MONTH, certification="none")
    check = report.check("level_grain")
    assert check is not None and check.status == "fail"
    assert "latest snapshot" in (check.reason or "")
    assert report.grade == "partial"
    assert (report.partial_reason or "").startswith("level_grain")


def test_extract_and_bigquery_truncs_are_buckets_too() -> None:
    model = _balances()
    sql = (
        "SELECT EXTRACT(year FROM balances.day) AS y, SUM(balances.balance) AS b "
        "FROM fin.balances AS balances GROUP BY 1"
    )
    assert _level_grain(sql, model).status == "fail"
    model.dialect = "bigquery"
    sql = (
        "SELECT DATE_TRUNC(balances.day, QUARTER) AS q, SUM(balances.balance) AS b "
        "FROM fin.balances AS balances GROUP BY 1"
    )
    assert _level_grain(sql, model).status == "fail"


def test_the_compiled_latest_per_bucket_form_passes() -> None:
    sql = compile_intent(
        QueryIntent(entity="balances", metrics=["balance"], grain="month"), _balances()
    )
    assert "MAX(" in sql
    assert _level_grain(sql, _balances()).status == "pass"


def test_a_window_ordered_by_the_snapshot_field_passes() -> None:
    sql = (
        "SELECT DATE_TRUNC('month', balances.day) AS month, SUM(balances.balance) AS b "
        "FROM fin.balances AS balances QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY balances.account_id, DATE_TRUNC('month', balances.day) "
        "ORDER BY balances.day DESC) = 1 GROUP BY 1"
    )
    assert _level_grain(sql, _balances()).status == "pass"


def test_a_table_whose_grain_is_the_bucket_has_nothing_to_collapse() -> None:
    model = _balances(grain="one row per account per month")
    assert _level_grain(_SUMMED_BY_MONTH, model).status == "pass"


_GLOBAL_PIN = (
    "SELECT DATE_TRUNC('month', balances.day) AS month, SUM(balances.balance) AS balance "
    "FROM fin.balances AS balances WHERE balances.day >= '2026-01-01' AND balances.day = "
    "(SELECT MAX(balances_latest.day) FROM fin.balances AS balances_latest "
    "WHERE balances_latest.day >= '2026-01-01') GROUP BY 1"
)
_PER_BUCKET_PIN = _GLOBAL_PIN.replace(
    "balances_latest.day >= '2026-01-01')",
    "balances_latest.day >= '2026-01-01' AND DATE_TRUNC('month', balances_latest.day) = "
    "DATE_TRUNC('month', balances.day))",
)


def test_a_level_asked_per_month_but_pinned_to_one_latest_day_fails() -> None:
    """The series collapsed to its last day: one row served as a year."""
    for question in (
        "Balance per month in 2026",
        "month-end balance by month in 2026",
        "balance at the end of each month of 2026",
    ):
        check = _level_grain(_GLOBAL_PIN, _balances(), question=question)
        assert check.status == "fail", question
        assert "one latest snapshot" in (check.reason or "")
    report = _report("Balance per month in 2026", _GLOBAL_PIN, certification="none")
    assert report.grade == "partial"
    assert (report.partial_reason or "").startswith("level_grain")


def test_a_level_pinned_per_bucket_passes_the_breakdown_ask() -> None:
    question = "Balance per month in 2026"
    assert _level_grain(_PER_BUCKET_PIN, _balances(), question=question).status == "pass"
    cte = (
        "WITH me AS (SELECT DATE_TRUNC('month', day) AS month, MAX(day) AS d "
        "FROM fin.balances GROUP BY 1) SELECT me.d AS day, SUM(b.balance) AS balance "
        "FROM me JOIN fin.balances AS b ON b.day = me.d GROUP BY 1"
    )
    assert _level_grain(cte, _balances(), question=question).status == "pass"
    # a question for one figure is answered by one snapshot
    assert _level_grain(_GLOBAL_PIN, _balances(), question="Balance in 2026").status == "pass"
    assert _level_grain(_GLOBAL_PIN, _balances(), question="Balance today").status == "pass"
    # the table's own grain is no excuse: a global pin collapses a monthly table too
    model = _balances(grain="one row per account per month")
    assert _level_grain(_GLOBAL_PIN, model, question=question).status == "fail"
    assert (
        _level_grain(_GLOBAL_PIN, _balances(), certification="certified", question=question).status
        == "skip"
    )


def test_a_flow_metric_is_untouched() -> None:
    assert _level_grain(_SUMMED_BY_MONTH, _balances(level=False)).status == "skip"


def test_certified_sql_is_not_re_read() -> None:
    assert _level_grain(_SUMMED_BY_MONTH, _balances(), certification="certified").status == "skip"


# ── the prompt ───────────────────────────────────────────────────────────────


def test_the_prompt_states_the_level_rule_per_bucket_only_when_a_level_metric_exists() -> None:
    prompt = serialize_model(_balances())
    assert "INSIDE EACH BUCKET" in prompt
    assert "never pin one MAX" in prompt
    assert "never one global latest row" in prompt
    assert "INSIDE EACH BUCKET" not in serialize_model(_balances(level=False))


def test_the_intent_rules_say_a_level_by_month_is_a_series() -> None:
    from services.runtime.intent_generator import _RULES

    assert "never one latest row" in _RULES
