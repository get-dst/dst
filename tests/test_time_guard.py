"""GROUPing BY a raw TIMESTAMP default_time_field
is a per-event grain wearing a per-period costume. Detection is deterministic and
biased hard against false positives — a legal grouping must never be blocked."""

from __future__ import annotations

from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime.time_guard import asked_period, grain_applied, raw_timestamp_groupings


def _model(*, time_type: str = "timestamp") -> SemanticModel:
    return SemanticModel(
        lens="web",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="analytics.sessions"),
                default_time_field="started_at",
                primary_key=["session_id"],
                fields=[
                    Field(name="session_id", type="integer"),
                    Field(name="started_at", type=time_type),  # type: ignore[arg-type]
                    Field(name="channel", type="string"),
                ],
            )
        ],
    )


def test_raw_timestamp_group_is_flagged() -> None:
    sql = (
        "SELECT sessions.started_at, count(*) FROM analytics.sessions AS sessions "
        "GROUP BY sessions.started_at"
    )
    assert raw_timestamp_groupings(sql, _model()) == [("sessions", "started_at")]


def test_unqualified_column_flags_only_in_single_table_scope() -> None:
    sole = "SELECT started_at, count(*) FROM analytics.sessions GROUP BY started_at"
    assert raw_timestamp_groupings(sole, _model()) == [("sessions", "started_at")]
    joined = (
        "SELECT started_at, count(*) FROM analytics.sessions AS s "
        "JOIN analytics.orders AS o ON s.session_id = o.session_id GROUP BY started_at"
    )
    assert raw_timestamp_groupings(joined, _model()) == []  # ambiguous — never guess


def test_date_typed_column_raw_grouped_is_legal_daily_grain() -> None:
    sql = "SELECT started_at, count(*) FROM analytics.sessions GROUP BY started_at"
    assert raw_timestamp_groupings(sql, _model(time_type="date")) == []


def test_truncated_timestamp_is_untouched() -> None:
    sql = (
        "SELECT DATE_TRUNC('month', started_at) AS period, count(*) "
        "FROM analytics.sessions GROUP BY DATE_TRUNC('month', started_at)"
    )
    assert raw_timestamp_groupings(sql, _model()) == []
    cast = "SELECT CAST(started_at AS DATE) AS d, count(*) FROM analytics.sessions GROUP BY 1"
    assert raw_timestamp_groupings(cast, _model()) == []


def test_no_grouping_is_untouched() -> None:
    assert raw_timestamp_groupings("SELECT max(started_at) FROM analytics.sessions", _model()) == []


def test_group_by_ordinal_resolves_to_projection() -> None:
    raw = "SELECT started_at, count(*) FROM analytics.sessions GROUP BY 1"
    assert raw_timestamp_groupings(raw, _model()) == [("sessions", "started_at")]
    bucketed = (
        "SELECT DATE_TRUNC('month', started_at) AS period, count(*) "
        "FROM analytics.sessions GROUP BY 1"
    )
    assert raw_timestamp_groupings(bucketed, _model()) == []


def test_group_by_alias_resolves_to_projection() -> None:
    bucketed = (
        "SELECT DATE_TRUNC('month', started_at) AS period, count(*) "
        "FROM analytics.sessions GROUP BY period"
    )
    assert raw_timestamp_groupings(bucketed, _model()) == []
    raw = "SELECT started_at AS period, count(*) FROM analytics.sessions GROUP BY period"
    assert raw_timestamp_groupings(raw, _model()) == [("sessions", "started_at")]


def test_primary_key_in_group_reads_as_deliberate_row_grain() -> None:
    sql = "SELECT session_id, started_at FROM analytics.sessions GROUP BY session_id, started_at"
    assert raw_timestamp_groupings(sql, _model()) == []


def test_inner_grouping_is_not_the_outer_grain() -> None:
    # An inner raw grouping can be deliberate pre-aggregation the outer query buckets.
    sql = (
        "SELECT DATE_TRUNC('month', ts) AS period, sum(n) FROM ("
        "SELECT started_at AS ts, count(*) AS n FROM analytics.sessions GROUP BY started_at"
        ") AS pre GROUP BY DATE_TRUNC('month', ts)"
    )
    assert raw_timestamp_groupings(sql, _model()) == []


def test_asked_period_reads_the_question() -> None:
    assert asked_period("How many sessions per month in 2025?") == "month"
    assert asked_period("daily active users this year") == "day"
    assert asked_period("weekly trend of signups") == "week"
    assert asked_period("quarterly bookings by segment") == "quarter"
    assert asked_period("annual revenue") == "year"
    assert asked_period("sessions over time") == "month"  # the default


def test_date_column_raw_grouped_under_a_monthly_ask_is_flagged() -> None:
    """A DATE column is legal daily grain — until the question asks a coarser
    unit. 'one row per month' served 273 daily rows under `verified`."""
    sql = "SELECT started_at, count(*) FROM analytics.sessions GROUP BY started_at"
    model = _model(time_type="date")
    assert raw_timestamp_groupings(sql, model, question="sessions by month in 2026") == [
        ("sessions", "started_at")
    ]
    assert raw_timestamp_groupings(sql, model, question="sessions per day in 2026") == []
    assert raw_timestamp_groupings(sql, model, question="daily sessions by month") == []
    assert raw_timestamp_groupings(sql, model, question="sessions in 2026") == []


def test_date_column_grouped_raw_beside_its_own_bucket_is_flagged_without_a_question() -> None:
    """GROUP BY DATE_TRUNC('month', d), d — the bucket is a no-op, at any grain."""
    sql = (
        "SELECT DATE_TRUNC('month', started_at) AS m, started_at, count(*) "
        "FROM analytics.sessions GROUP BY DATE_TRUNC('month', started_at), started_at"
    )
    assert raw_timestamp_groupings(sql, _model(time_type="date")) == [("sessions", "started_at")]


def test_grain_applied_reads_the_rows_not_the_sql() -> None:
    from datetime import date, timedelta

    daily = [[date(2026, 1, 1) + timedelta(days=i), i] for i in range(273)]
    check = grain_applied("VAT by month, one row per month", ["day", "vat"], daily, "none")
    assert check.status == "fail" and "273 distinct dates across 9 month(s)" in (check.reason or "")
    monthly = [[f"2026-{m:02d}-01", m] for m in range(1, 10)]
    assert grain_applied("VAT by month", ["day_month", "vat"], monthly, "none").status == "pass"
    # one month-end label per bucket, jittered per group, is one row per bucket
    ends = [[date(2026, m, 28), m] for m in range(1, 10)] + [
        [date(2026, m, 27), m] for m in range(1, 10)
    ]
    assert grain_applied("MRR per month", ["month_end", "mrr"], ends, "none").status == "pass"
    assert grain_applied("VAT in September", ["day", "vat"], daily, "none").status == "skip"
    assert grain_applied("VAT by month", ["vat"], [[1.0]], "none").status == "skip"
    assert grain_applied("VAT by month", ["day", "vat"], daily, "certified").status == "skip"
