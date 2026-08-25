"""SQL → governed scope decomposition (services/runtime/scope.py)."""

from __future__ import annotations

from services.runtime.scope import parse_scope


def test_leaderboard_query_decomposes() -> None:
    sql = (
        "SELECT REP_NAME, TOTAL_WON_EUR, TOTAL_WON_EUR * 0.10 AS commission_eur "
        "FROM NORTHWIND.GOLD.REP_LEADERBOARD ORDER BY TOTAL_WON_EUR DESC"
    )
    scope = parse_scope(sql)
    assert scope is not None
    assert scope.tables == ["NORTHWIND.GOLD.REP_LEADERBOARD"]
    assert "REP_NAME" in scope.fields
    assert "TOTAL_WON_EUR * 0.10 AS commission_eur" in scope.fields
    assert scope.filters == []  # no WHERE — surfaced as "no filter"
    assert scope.order_by == ["TOTAL_WON_EUR DESC"]


def test_filters_split_on_top_level_and() -> None:
    scope = parse_scope("SELECT a, sum(b) c FROM t WHERE region = 'EU' AND year = 2024 GROUP BY a")
    assert scope is not None
    assert scope.filters == ["region = 'EU'", "year = 2024"]


def test_or_filter_stays_intact() -> None:
    scope = parse_scope("SELECT * FROM t WHERE status = 'won' OR status = 'lost'")
    assert scope is not None
    assert scope.filters == ["status = 'won' OR status = 'lost'"]


def test_cte_name_is_not_a_governed_table() -> None:
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    scope = parse_scope(sql)
    assert scope is not None
    assert scope.tables == ["orders"]  # the CTE alias 'recent' is excluded


def test_unparseable_or_empty_returns_none() -> None:
    assert parse_scope(None) is None
    assert parse_scope("   ") is None
    assert parse_scope("this is not sql ;;;") is None
