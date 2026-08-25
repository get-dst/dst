"""Schemas validate fixtures; fakes satisfy the protocols."""

from __future__ import annotations

import pytest

from services.contracts import (
    Connector,
    Embedder,
    Entity,
    EntitySource,
    Field,
    LensConfig,
    LLMProvider,
    QueryGenerator,
    QueryResponse,
    SemanticModel,
    TraceLog,
)
from services.contracts.fakes import (
    EchoQueryGenerator,
    FakeConnector,
    HashEmbedder,
    ScriptedLLM,
)
from services.contracts.protocols import CacheableBlock, Message


def _sample_model() -> SemanticModel:
    return SemanticModel(
        lens="churn_risk",
        dialect="duckdb",
        entities=[
            Entity(
                name="accounts",
                source=EntitySource(connection="duck", table="main.mart_accounts"),
                fields=[
                    Field(name="account_id", type="string"),
                    Field(name="arr", type="number"),
                ],
            )
        ],
    )


def test_semantic_model_allow_lists() -> None:
    sm = _sample_model()
    assert sm.allowed_tables() == {"main.mart_accounts"}
    assert sm.allowed_columns() == {"main.mart_accounts": {"account_id", "arr"}}


def test_lens_config_roundtrip() -> None:
    raw = {
        "name": "churn_risk",
        "display_name": "Churn Risk",
        "connections": ["duck"],
        "access": {"allow": [{"caller": "churn_agent"}]},
    }
    cfg = LensConfig.model_validate(raw)
    assert cfg.name == "churn_risk"
    # No `model:` block → unset, NOT a vendor default (see test_byok_model_default).
    assert cfg.model.provider is None and cfg.model.model is None
    assert cfg.access.allow[0].caller == "churn_agent"
    # lossless JSON round-trip
    assert LensConfig.model_validate_json(cfg.model_dump_json()) == cfg


def test_eval_gate_blocks_by_default() -> None:
    # A dbt test is blocking by default, and so is the eval gate.
    # A fresh lens is safe under "block": no corpus / no servable model degrades
    # to a loud publish_gate skip (gated=False), never a refused publish.
    assert (
        LensConfig.model_validate(
            {"name": "x", "display_name": "X", "connections": ["d"]}
        ).eval_gate
        == "block"
    )


def test_answer_mode_drives_generation_temperature() -> None:
    from services.contracts.lens_config import ModelConfig

    assert ModelConfig().answer_mode == "balanced"  # default
    assert ModelConfig(answer_mode="strict").generation_temperature() == 0.0
    # balanced generates at 0.0 too: SQL generation is a
    # computation — sampled generation drifts counts across identical asks and
    # makes the eval gate flaky. The DEFAULT mode must be deterministic.
    assert ModelConfig(answer_mode="balanced").generation_temperature() == 0.0
    assert ModelConfig(answer_mode="exploratory").generation_temperature() == 0.5
    assert ModelConfig(temperature=0.3).generation_temperature() == 0.3  # explicit wins
    # round-trips through the lens config
    cfg = LensConfig.model_validate(
        {"name": "x", "display_name": "X", "connections": ["d"], "model": {"answer_mode": "strict"}}
    )
    assert LensConfig.model_validate_json(cfg.model_dump_json()).model.answer_mode == "strict"


def test_max_repairs_defaults_to_one_and_is_bounded() -> None:
    from pydantic import ValidationError

    from services.contracts.lens_config import ModelConfig

    assert ModelConfig().max_repairs == 1  # existing lenses keep today's budget
    assert ModelConfig(max_repairs=0).max_repairs == 0
    assert ModelConfig(max_repairs=3).max_repairs == 3
    for bad in (-1, 4):
        with pytest.raises(ValidationError):
            ModelConfig(max_repairs=bad)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeConnector(), Connector)
    assert isinstance(ScriptedLLM(), LLMProvider)
    assert isinstance(HashEmbedder(), Embedder)
    assert isinstance(EchoQueryGenerator(), QueryGenerator)


def test_scripted_llm_returns_in_order() -> None:
    llm = ScriptedLLM(["a", "b"])
    call = dict(system=[CacheableBlock("sys")], model="m", temperature=0.0, max_tokens=10)
    assert llm.complete(messages=[Message("user", "x")], **call).text == "a"
    assert llm.complete(messages=[Message("user", "y")], **call).text == "b"
    assert llm.complete(messages=[Message("user", "z")], **call).text == "b"  # last repeats


def test_hash_embedder_deterministic() -> None:
    emb = HashEmbedder()
    v1 = emb.embed(["hello"])[0]
    v2 = emb.embed(["hello"])[0]
    v3 = emb.embed(["world"])[0]
    assert len(v1) == emb.dim == 1024
    assert v1 == v2
    assert v1 != v3


def test_response_and_trace_construct() -> None:
    resp = QueryResponse(lens="churn_risk", answer="42", request_id="req_1")
    assert resp.confidence is None
    trace = TraceLog(
        request_id="req_1",
        org_id="org_1",
        lens="churn_risk",
        caller="churn_agent",
        question="how many?",
        status="ok",
    )
    assert trace.sample is None  # off by default


def test_explicit_temperature_is_honoured_not_silently_ignored() -> None:
    # `temperature: 0.0` in a lens file must not be dead config: if
    # generation_temperature() returns the answer_mode's value unconditionally, a
    # lens samples at 0.2 while its lens.yaml says 0.0 and refuses some fraction of
    # identical questions. `answer_mode: strict` is not a workaround — it also
    # switches on the inline judge and the adversary.
    from services.contracts.lens_config import ModelConfig

    assert ModelConfig().temperature is None  # unset = follow the mode
    assert ModelConfig().generation_temperature() == 0.0  # deterministic default
    assert ModelConfig(temperature=0.0).generation_temperature() == 0.0
    # an explicit value beats the mode in both directions
    assert ModelConfig(answer_mode="exploratory", temperature=0.0).generation_temperature() == 0.0
    assert ModelConfig(answer_mode="strict", temperature=0.5).generation_temperature() == 0.5
    # and it survives the JSON round-trip a stored lens bundle goes through
    cfg = LensConfig.model_validate(
        {
            "name": "x",
            "display_name": "X",
            "connections": ["d"],
            "model": {"temperature": 0.0},
        }
    )
    revived = LensConfig.model_validate_json(cfg.model_dump_json())
    assert revived.model.generation_temperature() == 0.0
