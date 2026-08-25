"""The answer contract — the output-grid default in every generation prompt.

A whole class of misses is CORRECT values in the WRONG grid (year groupings
rendered as dates, working columns left in the final SELECT), and per-project
convention prose is the only other defense. The contract ships in the shared
assemble() path, so serving and every eval lane carry it identically;
lens.yaml `model.answer_contract: off` removes it. Prompt-side only — no guard.
"""

from __future__ import annotations

from typing import Literal

import pytest

from services.certify.store import CertifiedAnswer
from services.contracts.fakes import ScriptedLLM
from services.contracts.lens_config import LensConfig, ModelConfig
from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import QueryResult
from services.lenses.store import LensBundle
from services.llm.registry import ResolvedModel
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import (
    ANSWER_CONTRACT,
    ANSWER_CONTRACT_SOURCE,
    assemble,
    eval_harness,
    question_harness,
)

_ORG = "00000000-0000-0000-0000-000000000000"
_Q = "total volume per year?"


@pytest.fixture(autouse=True)
def _no_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    # No embedder → retrieval/certified lookup no-op and nothing touches the DB
    # (the bundle has no connections or certified_dir either); an
    # ambient embedding key would otherwise turn assemble() into a live call.
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)


def _bundle(contract: Literal["strict", "off"] = "strict") -> LensBundle:
    return LensBundle(
        config=LensConfig(
            name="grid", display_name="Grid", model=ModelConfig(answer_contract=contract)
        ),
        semantic_model=SemanticModel(lens="p22", dialect="duckdb"),
    )


def test_contract_present_by_default_as_the_last_block() -> None:
    assembled = assemble(_bundle(), _Q, _ORG)
    assert assembled.prose[-1].text == ANSWER_CONTRACT
    assert assembled.prose[-1].source == "answer contract"
    # The contract is an instruction, not retrieved context — the visibility
    # count must not claim a chunk was retrieved on a lens with no context.
    assert assembled.counts["context_chunks"] == 0


def test_answer_contract_off_removes_the_block() -> None:
    assembled = assemble(_bundle("off"), _Q, _ORG)
    assert all(c.source != "answer contract" for c in assembled.prose)


def test_contract_identical_across_serving_and_eval_lanes() -> None:
    bundle = _bundle()
    model = ResolvedModel(ScriptedLLM(), "fake", "m")
    serving = assemble(bundle, _Q, _ORG)  # the serving call (query.py)
    assemble_q, _ = question_harness(bundle, _ORG, model)  # runner lanes
    assemble_for, _ = eval_harness(bundle, _ORG, model)  # certified suite
    answer = CertifiedAnswer(id="a1", lens="p22", question=_Q, sql="SELECT 1", created_by="t")
    lanes = [serving, assemble_q(_Q), assemble_for(answer)]
    assert [lane.prose[-1].text for lane in lanes] == [ANSWER_CONTRACT] * 3
    assert {lane.prose[-1].source for lane in lanes} == {"answer contract"}


def test_contract_never_cited_as_a_source() -> None:
    # The contract rides prose_context into generation, but it is an instruction,
    # not provenance — the composer must not surface it as a context citation.
    assembled = assemble(_bundle(), _Q, _ORG)
    ans = AnswerComposer(ScriptedLLM(["42 total."])).compose(
        question=_Q,
        generated=GeneratedQuery(sql="SELECT 1"),
        result=QueryResult(columns=["n"], rows=[[1]]),
        semantic_model=assembled.model,
        prose_context=assembled.prose,
    )
    assert all(c.ref != ANSWER_CONTRACT_SOURCE for c in ans.citations)


def test_contract_text_carries_the_three_clauses() -> None:
    # A gutting edit must not slip through as "still present": pin the exact
    # projection, grain, and precision language the wrong-grid misses call for.
    for phrase in (
        "exactly the quantities the question names",
        "grouping keys",
        "working columns stay in CTEs",
        "grain the question asks",
        "year value",
        "not a date",
        "full numeric precision",
    ):
        assert phrase in ANSWER_CONTRACT
