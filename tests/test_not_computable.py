"""The authored 'this lens cannot compute X' lever.

A lens genuinely lacking a measure used to SUBSTITUTE the nearest available
column and answer confidently about a different measure — and no declaration
could prevent it (prose is advisory, excluded_metrics needs a dropped metric,
population bounds rows not measures). Two deterministic rails now enforce the
declaration: the question rail (word-boundary on measure + aliases) and the
ANSWER-ECHO backstop — the model's own narration translates paraphrases back
to the canonical term ('The lifetime value of each referral varies…'), which
is the hook no phrase list gives the question side.
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


def _model() -> SemanticModel:
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


def _serve(question: str, tmp_path: Path, *, llm_scripts: list[str]):
    llm = ScriptedLLM(llm_scripts)
    return run_query(
        question=question,
        lens_name="partner_funnel",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
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


def test_a_paraphrase_is_caught_by_the_answer_echo(tmp_path: Path) -> None:
    """'how much is a referral worth over its life' names no declared surface —
    the model's answer does, and the serve refuses instead of substituting."""
    sql = '{"sql": "SELECT referrals.referral_id, referrals.amount FROM referrals AS referrals"}'
    res = _serve(
        "How much is a referral worth over its life?",
        tmp_path,
        llm_scripts=[sql, "The lifetime value of each referral varies widely, from 100 to 250."],
    )
    assert res.trace.status == "refused"
    assert "cannot compute lifetime value" in (res.response.answer or "")
    assert res.response.data is None  # the substituted answer never serves


def test_a_computable_question_is_unaffected(tmp_path: Path) -> None:
    sql = '{"sql": "SELECT COUNT(*) AS n FROM referrals AS referrals"}'
    res = _serve(
        "How many referrals do we have?",
        tmp_path,
        llm_scripts=[sql, "There are 2 referrals."],
    )
    assert res.trace.status == "ok"
    assert "cannot compute" not in (res.response.answer or "")
