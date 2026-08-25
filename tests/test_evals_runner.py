"""The health runner judges a live answer (there is no value-case regression
lane: certified answers are the regression suite, pinned in
test_certified_suite.py; behavioral shape pins live in test_evals_behavioral.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.eval import EvalCase
from services.contracts.fakes import ScriptedLLM
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.evals import runner
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs
from services.runtime.generator import GroundedSQLGenerator


def _model() -> SemanticModel:
    return SemanticModel(
        lens="customer_value",
        dialect="duckdb",
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="jaffle", table="customers"),
                fields=[
                    Field(name="customer_id", type="integer"),
                    Field(name="customer_lifetime_value", type="number"),
                    Field(name="number_of_orders", type="integer"),
                ],
            )
        ],
    )


def _adapt(kw: dict):
    """Old-style (model=, generator=) call shape → the harness API."""
    model = kw.pop("model")
    generator = kw.pop("generator")
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
    )
    kw.update(
        lens=model.lens,
        assemble_for=lambda _q: assembled,
        generators_for=lambda _a: (generator, None),
    )
    return kw


def _health(**kw):
    return runner.run_health(**_adapt(kw))


def _case() -> EvalCase:
    return EvalCase(
        id="c1",
        lens="customer_value",
        question="how many customers?",
        expected_sql="SELECT count(*) AS n FROM customers",
        source="authored",
        status="approved",
    )


@pytest.fixture
def conn(tmp_path: Path) -> DuckDBConnector:
    dst = tmp_path / "wh.duckdb"
    shutil.copy(settings.duckdb_jaffle_path, dst)
    return DuckDBConnector(str(dst))


def test_health_judges_a_live_answer(conn: DuckDBConnector) -> None:
    gen_json = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    gen_llm = ScriptedLLM([gen_json])
    comp_llm = ScriptedLLM(["There are 100 customers."])
    judge_llm = ScriptedLLM(['{"verdict": "approve", "reasoning": "matches the reference"}'])
    out = _health(
        connector=conn,
        model=_model(),
        cases=[_case()],
        generator=GroundedSQLGenerator(gen_llm),
        composer=AnswerComposer(comp_llm),
        judge_llm=judge_llm,
    )
    assert out.run.mode == "health"
    assert out.run.passed == 1
    assert out.results[0].grade == "approve" and out.results[0].passed
