"""An unconfirmed zero is never narrated as a business fact.

The same unresolved-empty event used to coin-flip between a zero asserted as
fact ("zero active paying customers are using AI replies") and an honest
"I cannot provide a count" — whichever the composer model wrote. The narration
is now ONE deterministic code path: when the empty-result investigation cannot
confirm the zero, no model writes the
sentence, the text says the zero is unconfirmed and names the literal date
filter (the usual cause), and the empty_result_investigation check
still fails so the grade stays below verified. A CONFIRMED zero keeps
composing normally — a real zero must not be hedged into uselessness.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime.answer import AnswerComposer
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.value_guard import unconfirmed_zero_text


def _model() -> SemanticModel:
    return SemanticModel(
        lens="adoption",
        dialect="duckdb",
        entities=[
            Entity(
                name="usage",
                source=EntitySource(connection="c", table="usage"),
                fields=[
                    Field(name="account_id", type="string"),
                    Field(name="status_date", type="string"),
                ],
            )
        ],
    )


def _warehouse(tmp_path: Path) -> DuckDBConnector:
    """30 distinct snapshot dates — past the probe's enumeration cap, so a
    probe of status_date is attempted but UNRESOLVED (a hardcoded
    today-literal against a lagging snapshot)."""
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE usage (account_id VARCHAR, status_date VARCHAR)")
    for day in range(1, 31):
        con.execute(f"INSERT INTO usage VALUES ('a{day}', '2026-07-{day:02d}')")
    con.close()
    return DuckDBConnector(str(tmp_path / "wh.duckdb"))


def _gen(sql: str) -> GroundedSQLGenerator:
    return GroundedSQLGenerator(ScriptedLLM(['{"sql": "' + sql + '"}'] * 3))


_TODAY_LITERAL_SQL = (
    "SELECT COUNT(DISTINCT usage.account_id) AS n FROM usage WHERE usage.status_date = '2026-08-23'"
)


def test_an_unconfirmed_zero_is_not_stated_as_a_fact(tmp_path: Path) -> None:
    composer = AnswerComposer(ScriptedLLM(["Zero accounts are using the feature."]), model="m")
    res = run_query(
        question="How many accounts use the feature right now?",
        lens_name="adoption",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(_TODAY_LITERAL_SQL),
        composer=composer,
    )
    assert res.trace.status == "ok"
    answer = res.response.answer or ""
    # The composer's assertive sentence never ships; the deterministic
    # narration does, and it discloses the likely cause.
    assert "Zero accounts are using the feature." not in answer
    assert "cannot confirm" in answer
    assert "2026-08-23" in answer and "filter" in answer.lower()
    # The verification signal that forced this is still on the report.
    checks = {
        c.name: c.status
        for c in (res.response.verification.checks if res.response.verification else [])
    }
    assert checks.get("empty_result_investigation") == "fail"


def test_a_confirmed_zero_still_composes_plainly(tmp_path: Path) -> None:
    """The literal exists (a real date), the zero is the data's answer — the
    composer writes it, unhedged."""
    con = duckdb.connect(str(tmp_path / "wh2.duckdb"))
    con.execute("CREATE TABLE usage (account_id VARCHAR, status_date VARCHAR)")
    con.execute("INSERT INTO usage VALUES ('a1', '2026-08-20'), ('a2', '2026-08-21')")
    con.close()
    connector = DuckDBConnector(str(tmp_path / "wh2.duckdb"))
    composer = AnswerComposer(ScriptedLLM(["Zero accounts on that date."]), model="m")
    res = run_query(
        question="How many accounts on 2026-08-20 had no usage?",
        lens_name="adoption",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=connector,
        generator=_gen(
            "SELECT COUNT(DISTINCT usage.account_id) AS n FROM usage "
            "WHERE usage.status_date = '2026-08-21' AND usage.account_id = 'a1'"
        ),
        composer=composer,
    )
    assert res.trace.status == "ok"
    assert "Zero accounts on that date." in (res.response.answer or "")


def test_the_narration_names_the_date_filter() -> None:
    text = unconfirmed_zero_text(_TODAY_LITERAL_SQL, "duckdb")
    assert "cannot confirm" in text
    assert "`status_date` = '2026-08-23'" in text
    assert "snapshot" in text


def test_the_narration_survives_unparseable_sql() -> None:
    text = unconfirmed_zero_text("NOT SQL AT ALL ((", None)
    assert "cannot confirm" in text


def test_generation_prompts_forbid_the_today_literal() -> None:
    from services.runtime.generator import serialize_model
    from services.runtime.intent_generator import serialize_layer

    assert "LATEST AVAILABLE" in serialize_model(_model())
    assert "LATEST AVAILABLE" in serialize_layer(_model())
