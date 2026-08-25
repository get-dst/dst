"""GROUPing BY a raw TIMESTAMP default_time_field
is a per-event grain wearing a per-period costume. Detection is deterministic and
biased hard against false positives — a legal grouping must never be blocked."""

from __future__ import annotations

from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime.time_guard import asked_period, raw_timestamp_groupings


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
