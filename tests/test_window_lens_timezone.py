"""A time window on a timestamp column is bounded in the lens's clock, not the server's.

The failure: "How many pro matches were played last month?" filtered a
timestamp-with-time-zone column with plain date literals (`>= '2026-08-01'`).
The warehouse reads such a literal in its session's zone, so the same compiled
query counted 439 matches on a server in UTC and 438 on one in Europe/Paris,
although the lens declares `timezone: UTC`.

Pinned here: the window's bounds carry the lens zone's offset, so a
zone-aware column is cut at the lens's midnight whatever the session zone is,
and a zone-less column still compares wall clock to wall clock, exactly as
before. Both dialects that compile windows here, DuckDB and Postgres, run the
same query under two session zones and must agree; the compiled SQL is the
same under two process zones.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import date

import duckdb
import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedResolver
from tests.test_query_api import needs_db

QUESTION = "How many pro matches were played last month?"
TODAY = date(2026, 9, 15)
ZONES = ("UTC", "Europe/Paris")
# Instants in UTC. Last month in UTC holds the last three; a reading of the
# bounds in Europe/Paris (UTC+2 in summer) holds the first two instead.
EVENTS = [
    "2026-07-31 23:30:00",
    "2026-08-15 12:00:00",
    "2026-08-31 22:30:00",
    "2026-08-31 23:30:00",
    "2026-09-01 00:30:00",
]
IN_UTC = 3


def _model(dialect: str, tz: str = "UTC") -> SemanticModel:
    return SemanticModel(
        lens="pro",
        dialect=dialect,  # type: ignore[arg-type]
        timezone=tz,
        entities=[
            Entity(
                name="pro_matches",
                source=EntitySource(connection="wh", table="pro_matches"),
                default_time_field="start_time",
                fields=[
                    Field(name="match_id", type="integer"),
                    Field(name="start_time", type="timestamp"),
                ],
                metrics=[Metric(name="matches", agg="count", expr="pro_matches.match_id")],
            )
        ],
    )


class _Counts:
    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        chosen = next((p for p in ("aggregate", "matches") if p in names), None)
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="t")


def _sql(dialect: str, tz: str = "UTC") -> str:
    model = _model(dialect, tz)
    res = TypedResolver(_Counts(), today=TODAY).resolve(QUESTION, model)  # type: ignore[arg-type]
    assert res.intent is not None, res.clarification
    assert [(f.field, f.op) for f in res.intent.filters] == [
        ("start_time", ">="),
        ("start_time", "<"),
    ]
    return compile_intent(res.intent, model)


@pytest.fixture
def process_zone() -> Iterator[None]:
    before = os.environ.get("TZ")
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def test_the_compiled_window_does_not_depend_on_the_process_zone(process_zone: None) -> None:
    seen = set()
    for zone in ZONES:
        os.environ["TZ"] = zone
        time.tzset()
        seen.add((_sql("duckdb"), _sql("postgres")))
    assert len(seen) == 1, seen


def _duckdb_count(sql: str, column_type: str, session_zone: str) -> int:
    con = duckdb.connect()
    con.execute(f"SET TimeZone = '{session_zone}'")
    con.execute(f"CREATE TABLE pro_matches (match_id INTEGER, start_time {column_type})")
    for i, at in enumerate(EVENTS):
        literal = f"TIMESTAMPTZ '{at}+00:00'" if column_type == "TIMESTAMPTZ" else f"'{at}'"
        con.execute(f"INSERT INTO pro_matches VALUES ({i}, {literal})")
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("column_type", ["TIMESTAMPTZ", "TIMESTAMP"])
def test_duckdb_counts_the_lens_month_under_any_session_zone(column_type: str) -> None:
    sql = _sql("duckdb")
    counts = {zone: _duckdb_count(sql, column_type, zone) for zone in ZONES}
    assert counts == dict.fromkeys(ZONES, IN_UTC), (sql, counts)


def test_a_non_utc_lens_zone_moves_the_bounds_to_its_own_midnight() -> None:
    sql = _sql("duckdb", "Europe/Paris")
    counts = {zone: _duckdb_count(sql, "TIMESTAMPTZ", zone) for zone in ZONES}
    assert counts == dict.fromkeys(ZONES, 2), (sql, counts)


def test_an_undeclared_zone_is_never_invented() -> None:
    """No declared clock, no offset: a bound in a zone nobody declared would be
    a silent assumption."""
    sql = _sql("duckdb", "")
    assert "'2026-08-01'" in sql and "+00:00" not in sql


@needs_db
@pytest.mark.parametrize("column_type", ["timestamptz", "timestamp"])
def test_postgres_counts_the_lens_month_under_any_session_zone(column_type: str) -> None:
    sql = _sql("postgres")
    counts: dict[str, int] = {}
    with create_engine(settings.database_admin_url).connect() as con:
        con.execute(
            text(f"CREATE TEMP TABLE pro_matches (match_id integer, start_time {column_type})")
        )
        for i, at in enumerate(EVENTS):
            value = f"{at}+00:00" if column_type == "timestamptz" else at
            con.execute(text("INSERT INTO pro_matches VALUES (:i, :at)"), {"i": i, "at": value})
        for zone in ZONES:
            con.execute(text(f"SET TIME ZONE '{zone}'"))
            counts[zone] = int(con.execute(text(sql)).scalar_one())
        con.rollback()
    assert counts == dict.fromkeys(ZONES, IN_UTC), (sql, counts)
