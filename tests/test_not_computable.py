"""The authored 'this lens cannot compute X' lever.

A lens genuinely lacking a measure used to SUBSTITUTE the nearest available
column and answer confidently about a different measure — and no declaration
could prevent it (prose is advisory, excluded_metrics needs a dropped metric,
population bounds rows not measures). Two deterministic rails now enforce the
declaration: the question rail (word-boundary on measure + aliases) and the
result-column backstop — SQL written for a paraphrase names its column after
the measure it computed (`amount AS lifetime_value`), which is the hook no
phrase list gives the question side. The answer's prose is never read: a
population sentence saying what is not counted names a measure it never
computed.
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

_NC = [
    {
        "measure": "lifetime value",
        "aliases": ["LTV"],
        "route_to": "partner_ltv",
        "reason": "lifetime_* lives on dim_partner_referral, not selected here",
    }
]


def _model(population: str = "") -> SemanticModel:
    return SemanticModel(
        lens="partner_funnel",
        dialect="duckdb",
        entities=[
            Entity(
                name="referrals",
                source=EntitySource(connection="c", table="referrals"),
                fields=[
                    Field(name="referral_id", type="string"),
                    Field(name="amount", type="number"),
                ],
                population=population or None,
            )
        ],
        not_computable=_NC,
    )


def _warehouse(tmp_path: Path) -> DuckDBConnector:
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE referrals (referral_id VARCHAR, amount DECIMAL(10,2))")
    con.execute("INSERT INTO referrals VALUES ('r1', 100.0), ('r2', 250.0)")
    con.close()
    return DuckDBConnector(str(tmp_path / "wh.duckdb"))


def _serve(
    question: str,
    tmp_path: Path,
    *,
    llm_scripts: list[str],
    population: str = "",
    structured: bool = False,
):
    llm = ScriptedLLM(llm_scripts)
    return run_query(
        question=question,
        lens_name="partner_funnel",
        org_id="org",
        caller="t",
        semantic_model=_model(population),
        connector=_warehouse(tmp_path),
        generator=GroundedSQLGenerator(llm),
        composer=None if structured else AnswerComposer(llm),
    )


def test_a_question_naming_the_measure_refuses_before_any_generation(tmp_path: Path) -> None:
    res = _serve(
        "What is the lifetime value of each partner referral?",
        tmp_path,
        llm_scripts=["SHOULD NEVER BE CALLED"],
    )
    assert res.trace.status == "refused"
    assert "cannot compute lifetime value" in (res.response.answer or "")
    assert "partner_ltv" in (res.response.answer or "")  # the route is one hop


def test_an_alias_refuses_too(tmp_path: Path) -> None:
    res = _serve("LTV per referral?", tmp_path, llm_scripts=["SHOULD NEVER BE CALLED"])
    assert res.trace.status == "refused"


def test_a_paraphrase_is_caught_by_the_result_columns(tmp_path: Path) -> None:
    """'how much is a referral worth over its life' names no declared surface —
    the SQL written for it does: its result column carries the measure's name,
    and the serve refuses instead of substituting, before any prose is composed."""
    sql = (
        '{"sql": "SELECT referrals.referral_id, referrals.amount AS lifetime_value '
        'FROM referrals AS referrals"}'
    )
    res = _serve(
        "How much is a referral worth over its life?",
        tmp_path,
        llm_scripts=[sql, "SHOULD NEVER BE CALLED"],
    )
    assert res.trace.status == "refused"
    assert "cannot compute lifetime value" in (res.response.answer or "")
    assert res.response.data is None  # the substituted answer never serves


def test_a_structured_serve_is_held_to_the_same_boundary(tmp_path: Path) -> None:
    """A rows-only serve composes no prose, so a check on prose never saw it: the
    result columns are the same on every format."""
    sql = (
        '{"sql": "SELECT SUM(referrals.amount) AS total_lifetime_value '
        'FROM referrals AS referrals"}'
    )
    res = _serve(
        "How much are referrals worth over their life?",
        tmp_path,
        llm_scripts=[sql],
        structured=True,
    )
    assert res.trace.status == "refused"
    assert "cannot compute lifetime value" in (res.response.answer or "")


def test_a_population_disclosure_naming_the_measure_serves(tmp_path: Path) -> None:
    """The measure named in prose is not the measure computed: an answer whose
    population sentence says what is NOT counted, to a question and SQL that
    never touch the measure, is an answer, not a substitution."""
    sql = '{"sql": "SELECT COUNT(*) AS n FROM referrals AS referrals"}'
    res = _serve(
        "How many referrals do we have?",
        tmp_path,
        llm_scripts=[
            sql,
            "There are 2 referrals. Only referrals with a recorded amount are counted; "
            "lifetime value is not tracked for them.",
        ],
        population="Referrals with a recorded amount; lifetime value is not counted.",
    )
    assert res.trace.status == "ok"
    assert "cannot compute" not in (res.response.answer or "")
    assert res.response.data is not None


def test_a_computable_question_is_unaffected(tmp_path: Path) -> None:
    sql = '{"sql": "SELECT COUNT(*) AS n FROM referrals AS referrals"}'
    res = _serve(
        "How many referrals do we have?",
        tmp_path,
        llm_scripts=[sql, "There are 2 referrals."],
    )
    assert res.trace.status == "ok"
    assert "cannot compute" not in (res.response.answer or "")
