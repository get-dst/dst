"""Date coverage: the asked period vs the table's MEASURED span.

The silent-zero shape this rail removes: data ends in May, someone asks about
September, the aggregate runs over zero rows and a confident 0 serves with
every check green — nothing in the result says the rows could not exist.
`window_ranges` resolves the closed window vocabulary to calendar spans;
`date_coverage_check` fires only when a resolved span lies ENTIRELY outside
the whole-table MIN/MAX of a time column the SQL actually filters on, and the
pipeline appends the disclosure to the answer itself. False positives are
forbidden: unresolvable windows, unmeasured tables, and unfiltered time
columns all skip."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from services.contracts.fakes import FakeConnector
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.lenses.profile_enrich import EntityCoverage
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.timewindow import temporal_terms, window_ranges
from services.runtime.verification import build_report, date_coverage_check

TODAY = date(2026, 9, 7)


# ── window_ranges ────────────────────────────────────────────────────────────


def test_explicit_periods_resolve_to_calendar_spans() -> None:
    assert window_ranges(frozenset({"2026"}), TODAY) == [(date(2026, 1, 1), date(2026, 12, 31))]
    assert window_ranges(temporal_terms("revenue in March 2026"), TODAY) == [
        (date(2026, 3, 1), date(2026, 3, 31))
    ]
    assert window_ranges(temporal_terms("how was Q3 2025?"), TODAY) == [
        (date(2025, 7, 1), date(2025, 9, 30))
    ]


def test_a_quarter_pairs_with_every_stated_year() -> None:
    got = set(window_ranges(temporal_terms("Q3 2025 vs Q3 2026"), TODAY))
    assert got == {
        (date(2025, 7, 1), date(2025, 9, 30)),
        (date(2026, 7, 1), date(2026, 9, 30)),
    }


def test_a_bare_quarter_does_not_resolve() -> None:
    # "Q3" alone could be any year — a guess here becomes a false accusation
    # downstream, so it is simply not a range.
    assert window_ranges(temporal_terms("how did Q3 look?"), TODAY) == []


def test_relative_windows_resolve_against_the_given_clock() -> None:
    assert window_ranges(frozenset({"this month"}), TODAY) == [
        (date(2026, 9, 1), date(2026, 9, 30))
    ]
    assert window_ranges(frozenset({"last month"}), TODAY) == [
        (date(2026, 8, 1), date(2026, 8, 31))
    ]
    assert window_ranges(frozenset({"next quarter"}), TODAY) == [
        (date(2026, 10, 1), date(2026, 12, 31))
    ]
    assert window_ranges(frozenset({"last year"}), TODAY) == [
        (date(2025, 1, 1), date(2025, 12, 31))
    ]


def test_trailing_windows_end_today() -> None:
    assert window_ranges(temporal_terms("last 30 days"), TODAY) == [(date(2026, 8, 8), TODAY)]
    [(start, end)] = window_ranges(temporal_terms("trailing 3 months"), TODAY)
    assert end == TODAY and start == date(2026, 6, 7)


# ── date_coverage_check ──────────────────────────────────────────────────────


def _model() -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection="wh", table="main.invoices"),
                default_time_field="invoice_date",
                fields=[
                    Field(name="invoice_date", type="date"),
                    Field(name="amount", type="number"),
                ],
            ),
            Entity(
                name="customers",
                source=EntitySource(connection="wh", table="main.customers"),
                fields=[Field(name="created_at", type="timestamp")],
            ),
        ],
    )


_COV = {
    "invoices": EntityCoverage(
        table="main.invoices",
        column="invoice_date",
        start=date(2025, 1, 2),
        end=date(2026, 5, 31),
        declared=True,
    )
}

_SQL_SEPT = (
    "SELECT SUM(amount) AS revenue FROM main.invoices "
    "WHERE invoice_date >= '2026-09-01' AND invoice_date < '2026-10-01'"
)
_SQL_MARCH = (
    "SELECT SUM(amount) AS revenue FROM main.invoices "
    "WHERE invoice_date >= '2026-03-01' AND invoice_date < '2026-04-01'"
)


def test_a_period_beyond_coverage_fails_naming_the_end_date() -> None:
    check = date_coverage_check("Revenue in September 2026?", _SQL_SEPT, _model(), _COV)
    assert check.status == "fail"
    assert "invoices data ends 2026-05-31" in (check.reason or "")


def test_a_period_inside_coverage_passes() -> None:
    check = date_coverage_check("Revenue in March 2026?", _SQL_MARCH, _model(), _COV)
    assert check.status == "pass"


def test_a_partially_covered_window_passes() -> None:
    # "this year" reaches past the coverage end but overlaps it — partial-period
    # framing is the consumer's judgment, not a coverage violation.
    sql = "SELECT SUM(amount) AS revenue FROM main.invoices WHERE invoice_date >= '2026-01-01'"
    check = date_coverage_check("Revenue this year?", sql, _model(), _COV)
    assert check.status == "pass"


def test_an_unfiltered_time_column_cannot_fire() -> None:
    # customers.created_at ends long ago, but the SQL never filters it — a
    # dimension table's clock must not flag a fact-table question.
    cov = dict(_COV)
    cov["customers"] = EntityCoverage(
        table="main.customers",
        column="created_at",
        start=date(2020, 1, 1),
        end=date(2024, 12, 31),
        declared=False,
    )
    sql = (
        "SELECT SUM(i.amount) AS revenue FROM main.invoices i "
        "JOIN main.customers c ON c.customer_id = i.customer_id "
        "WHERE i.invoice_date >= '2026-03-01' AND i.invoice_date < '2026-04-01'"
    )
    check = date_coverage_check("Revenue in March 2026?", sql, _model(), cov)
    assert check.status == "pass"


def test_history_before_a_declared_time_field_fires_begins() -> None:
    sql = (
        "SELECT SUM(amount) AS revenue FROM main.invoices "
        "WHERE invoice_date >= '2019-01-01' AND invoice_date < '2020-01-01'"
    )
    check = date_coverage_check("Revenue in 2019?", sql, _model(), _COV)
    assert check.status == "fail"
    assert "invoices data begins 2025-01-02" in (check.reason or "")


def test_an_undeclared_column_never_accuses_missing_history() -> None:
    # A load stamp's MIN says when loading started, not where history does —
    # without the author's declared time field the "begins" direction stays shut.
    cov = {"invoices": replace(_COV["invoices"], declared=False)}
    sql = (
        "SELECT SUM(amount) AS revenue FROM main.invoices "
        "WHERE invoice_date >= '2019-01-01' AND invoice_date < '2020-01-01'"
    )
    check = date_coverage_check("Revenue in 2019?", sql, _model(), cov)
    assert check.status == "pass"


def test_a_snapshot_names_its_as_of() -> None:
    cov = {
        "invoices": EntityCoverage(
            table="main.invoices",
            column="invoice_date",
            start=date(2026, 6, 5),
            end=date(2026, 6, 5),
            declared=True,
        )
    }
    check = date_coverage_check("Revenue in September 2026?", _SQL_SEPT, _model(), cov)
    assert check.status == "fail"
    assert "single snapshot as of 2026-06-05" in (check.reason or "")


def test_windowless_and_unmeasured_questions_skip() -> None:
    assert date_coverage_check("Total revenue?", _SQL_SEPT, _model(), _COV).status == "skip"
    assert (
        date_coverage_check("Revenue in September 2026?", _SQL_SEPT, _model(), {}).status == "skip"
    )
    # a stated window that resolves to no calendar span is a skip, not a guess
    check = date_coverage_check("Revenue in Q3?", _SQL_SEPT, _model(), _COV)
    assert check.status == "skip"


def test_certified_serve_beyond_coverage_grades_partial() -> None:
    # Approval vouches for the SQL's meaning, never for a period the warehouse
    # hasn't loaded — same demotion as freshness.
    report = build_report(
        question="Revenue in September 2026?",
        sql=_SQL_SEPT,
        answer_text="",
        result=QueryResult(columns=["revenue"], rows=[[0]]),
        semantic_model=_model(),
        definition_used=None,
        truncated=False,
        certification="certified",
        composed=False,
        uncomposed_reason="test",
        entity_coverage=_COV,
    )
    assert report.grade == "partial"
    check = report.check("date_coverage")
    assert check is not None and check.status == "fail"


# ── the pipeline says it out loud ────────────────────────────────────────────


def _serve(question: str, sql: str, cov: dict[str, EntityCoverage], value: object = 0):  # noqa: ANN202
    return run_query(
        question=question,
        lens_name="sales",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(result=QueryResult(columns=["revenue"], rows=[[value]])),
        generator=FixedSQLGenerator(sql),
        composer=None,
        entity_coverage=cov,
    )


def test_a_zero_beyond_coverage_is_disclosed_never_bare() -> None:
    res = _serve("Revenue in September 2026?", _SQL_SEPT, _COV)
    assert res.trace.status == "ok"  # served, not refused — but never silently
    assert "invoices data ends 2026-05-31" in res.response.answer
    assert res.response.confidence == "partial"
    report = res.response.verification
    assert report is not None
    check = report.check("date_coverage")
    assert check is not None and check.status == "fail"


def test_a_period_inside_coverage_serves_unchanged() -> None:
    res = _serve("Revenue in March 2026?", _SQL_MARCH, _COV, value=123.45)
    assert res.trace.status == "ok"
    assert "data ends" not in res.response.answer
    report = res.response.verification
    assert report is not None
    check = report.check("date_coverage")
    assert check is not None and check.status == "pass"


def test_a_snapshot_serve_carries_its_as_of() -> None:
    cov = {
        "invoices": EntityCoverage(
            table="main.invoices",
            column="invoice_date",
            start=date(2026, 6, 5),
            end=date(2026, 6, 5),
            declared=True,
        )
    }
    res = _serve("Revenue in September 2026?", _SQL_SEPT, cov)
    assert "single snapshot as of 2026-06-05" in res.response.answer
