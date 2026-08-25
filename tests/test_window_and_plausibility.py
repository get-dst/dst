"""window_applied and zero_ratio — the two plausibility checks that grounding
alone does not cover.

Neither is a faithfulness check: the number IS in the rows and grounding is
correct to pass. What they catch is the period never reaching the WHERE clause
(an all-history count served as a monthly figure) and an empty numerator
window composing a confident 0.0. Both cap at `partial`, never `unverified`,
and never withhold — a pre-windowed source view and a genuine zero are the
false-positive shapes they stay safe against."""

from __future__ import annotations

from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.verification import build_report


def _model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="bigquery",
        entities=[
            Entity(
                name="movements",
                source=EntitySource(connection="wh", table="marts.movements"),
                default_time_field="movement_date",
                fields=[Field(name="metric_type", type="string")],
            )
        ],
    )


def _report(question: str, sql: str, rows: list[list[object]], columns: list[str]):  # noqa: ANN202
    return build_report(
        question=question,
        sql=sql,
        answer_text="",
        result=QueryResult(columns=columns, rows=rows),
        semantic_model=_model(),
        definition_used=None,
        truncated=False,
        certification="none",
        composed=False,
        uncomposed_reason="rows-only test",
    )


# ── window_applied ───────────────────────────────────────────────────────────


def test_unwindowed_sql_for_a_windowed_question_is_not_verified() -> None:
    # The failure: "last month" generates SQL with no date predicate, and
    # all-history rows serve as a monthly figure at verified.
    report = _report(
        "How many customers churned last month?",
        "SELECT COUNT(DISTINCT account_id) AS churned FROM movements "
        "WHERE movements.metric_type = 'Month Over Month'",
        rows=[[8740]],
        columns=["churned"],
    )
    check = report.check("window_applied")
    assert check is not None and check.status == "fail"
    assert "last month" in (check.reason or "")
    assert report.grade == "partial"  # capped, never unverified — and never withheld


def test_a_date_predicate_satisfies_the_window_check() -> None:
    report = _report(
        "How many customers churned last month?",
        "SELECT COUNT(*) FROM movements WHERE movement_date >= "
        "DATE_TRUNC(DATE_SUB(CURRENT_DATE, INTERVAL 1 MONTH), MONTH)",
        rows=[[12]],
        columns=["n"],
    )
    check = report.check("window_applied")
    assert check is not None and check.status == "pass"


def test_a_declared_time_field_reference_counts_as_date_handling() -> None:
    # A pre-windowed predicate on the entity's own time field passes even
    # without a date function — the coarse check must not fail honest SQL.
    report = _report(
        "orders this quarter?",
        "SELECT COUNT(*) FROM movements WHERE movement_date = @period_start",
        rows=[[5]],
        columns=["n"],
    )
    check = report.check("window_applied")
    assert check is not None and check.status == "pass"


def test_a_windowless_question_skips_the_check() -> None:
    report = _report(
        "How many customers churned?",
        "SELECT COUNT(*) FROM movements",
        rows=[[99]],
        columns=["n"],
    )
    check = report.check("window_applied")
    assert check is not None and check.status == "skip"


# ── zero_ratio ───────────────────────────────────────────────────────────────


def test_exact_zero_from_dividing_sql_is_not_verified() -> None:
    # dau=0 over a lagging snapshot; SAFE_DIVIDE composes a confident 0.0 and
    # grounding correctly passes — the plausibility check is what demotes it.
    report = _report(
        "What is our DAU/MAU stickiness?",
        "SELECT SAFE_DIVIDE(dau, mau) AS stickiness FROM d, m",
        rows=[[0.0]],
        columns=["stickiness"],
    )
    check = report.check("zero_ratio")
    assert check is not None and check.status == "fail"
    assert "matched no rows" in (check.reason or "")
    assert report.grade == "partial"


def test_nonzero_ratio_passes() -> None:
    report = _report(
        "What is our DAU/MAU stickiness?",
        "SELECT SAFE_DIVIDE(dau, mau) AS stickiness FROM d, m",
        rows=[[0.42]],
        columns=["stickiness"],
    )
    check = report.check("zero_ratio")
    assert check is not None and check.status == "pass"


def test_zero_count_without_division_is_not_flagged() -> None:
    # A plain COUNT of zero is the value_guard/has_rows story, not this check's.
    report = _report(
        "How many churned?",
        "SELECT COUNT(*) AS n FROM movements WHERE metric_type = 'x'",
        rows=[[0]],
        columns=["n"],
    )
    check = report.check("zero_ratio")
    assert check is not None and check.status == "skip"


def test_multi_row_results_skip_the_ratio_check() -> None:
    report = _report(
        "stickiness by region?",
        "SELECT region, SAFE_DIVIDE(dau, mau) FROM d GROUP BY region",
        rows=[["EU", 0.0], ["US", 0.4]],
        columns=["region", "stickiness"],
    )
    check = report.check("zero_ratio")
    assert check is not None and check.status == "skip"
