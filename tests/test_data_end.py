"""An empty answer for a period the data has not reached yet says so.

"Fee revenue today", asked before today's visits are loaded, ran over no rows
and served an empty figure. When the result holds no data, one live MAX of the
filtered time column says where the data ends, and a window beginning after it
is answered with that, not with the empty result. A window inside the data
that happens to be empty still serves.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.contracts.warehouse import QueryResult
from services.runtime.data_end import no_data
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
from tests.test_typed_question_shapes import DOMAINS, _model, _Prefers


@pytest.fixture
def gym(tmp_path) -> str:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "gym.duckdb")
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE visits AS SELECT * FROM (VALUES "
        "('v1', 'm1', 'Aino', 'harbour wall', 'day pass', DATE '2026-10-19', 12.0),"
        "('v2', 'm2', 'Bruno', 'old mill', 'monthly pass', DATE '2026-10-21', 45.0)"
        ") AS t(visit_id, member_id, member_name, gym, pass_type, visited_on, fee)"
    )
    con.close()
    return path


def _serve(path: str, question: str, generator):  # type: ignore[no-untyped-def]
    return run_query(
        question=question,
        lens_name="climbing",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=DuckDBConnector(path),
        generator=generator,
        composer=None,
    )


def _typed(today: date) -> TypedIntentGenerator:
    resolver = TypedResolver(
        _Prefers("aggregate", "fee_revenue", "visit_count"),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=today,
    )
    return TypedIntentGenerator(resolver)


def test_today_before_todays_rows_says_where_the_data_ends(gym: str) -> None:
    res = _serve(gym, "What was fee revenue today?", _typed(date(2026, 10, 22)))
    assert res.response.status == "refused", res.response.answer
    assert "runs to 2026-10-21" in res.response.answer
    assert "begins 2026-10-22" in res.response.answer


def test_a_count_of_zero_after_the_data_ends_says_so_too(gym: str) -> None:
    res = _serve(gym, "How many visits were there today?", _typed(date(2026, 10, 23)))
    assert res.response.status == "refused", res.response.answer
    assert "runs to 2026-10-21" in res.response.answer


def test_a_generated_query_reads_the_window_from_the_question(gym: str) -> None:
    sql = "SELECT SUM(fee) AS fee_revenue FROM visits WHERE visited_on = DATE '2026-10-22'"
    res = _serve(gym, "Fee revenue on 2026-10-22?", FixedSQLGenerator(sql))
    assert res.response.status == "refused", res.response.answer
    assert "runs to 2026-10-21" in res.response.answer


def test_an_empty_day_inside_the_data_still_serves(gym: str) -> None:
    res = _serve(gym, "What was fee revenue on 2026-10-20?", _typed(date(2026, 10, 22)))
    assert res.response.status == "ok", res.response.answer


def test_no_data_reads_the_shapes_of_nothing() -> None:
    sql = "SELECT SUM(fee) AS fee_revenue FROM visits"
    assert no_data(sql, "duckdb", QueryResult(columns=["fee_revenue"], rows=[]))
    assert no_data(sql, "duckdb", QueryResult(columns=["fee_revenue"], rows=[[None]]))
    assert not no_data(sql, "duckdb", QueryResult(columns=["fee_revenue"], rows=[[0]]))
    ratio = "SELECT SUM(a) / NULLIF(SUM(b), 0) AS share FROM visits"
    assert no_data(ratio, "duckdb", QueryResult(columns=["share"], rows=[[None]]))
