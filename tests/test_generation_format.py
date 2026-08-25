"""The generator's structured-output contract.

The old behavior — any unparseable reply silently became "raw SQL" — executed
prose as SQL and burned the pipeline's one repair on a formatting slip. Now:
bare SQL stays accepted (a common, usable model slip), everything else re-asks
with the problem named (≤2), and only then degrades to the guard's rejection.
"""

from __future__ import annotations

import pytest

from services.contracts.fakes import ScriptedLLM
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.contracts.semantic_model import Entity, EntitySource, SemanticModel
from services.runtime.generator import (
    GenerationFormatError,
    GroundedSQLGenerator,
    parse_generation,
)

_MODEL = SemanticModel(
    lens="t",
    dialect="postgres",
    entities=[Entity(name="orders", source=EntitySource(connection="wh", table="orders"))],
)

_VALID = '{"sql": "SELECT id FROM orders", "definition_used": null, "rationale": "count"}'


# ── parse_generation ─────────────────────────────────────────────────────────


def test_bare_sql_is_accepted() -> None:
    assert parse_generation("SELECT id FROM orders").sql == "SELECT id FROM orders"
    assert parse_generation("```sql\nWITH t AS (SELECT 1) SELECT * FROM t\n```").sql.startswith(
        "WITH"
    )


@pytest.mark.parametrize(
    "reply",
    [
        "Sure! Here is the query you asked for.",  # prose
        '"just a JSON string"',  # JSON but not an object
        '{"rationale": "no sql key"}',  # envelope without sql
        '{"sql": ""}',  # empty sql string
        '{"sql": 42}',  # sql not a string
    ],
)
def test_malformed_replies_raise(reply: str) -> None:
    with pytest.raises(GenerationFormatError):
        parse_generation(reply)


# ── the re-ask loop ──────────────────────────────────────────────────────────


class _Recording(ScriptedLLM):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.turns: list[list[Message]] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.turns.append(list(messages))
        return super().complete(
            system=system,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def _generate(llm: _Recording):
    gen = GroundedSQLGenerator(llm, model="m")
    return gen.generate(
        question="how many orders?", semantic_model=_MODEL, prose_context=[], dialect="postgres"
    )


def test_reask_converges_and_accumulates_tokens() -> None:
    llm = _Recording(["Let me think about that.", _VALID])
    gq = _generate(llm)
    assert gq.sql == "SELECT id FROM orders"
    assert len(llm.turns) == 2
    # The re-ask names the problem and appends turns (cached prefix intact).
    followup = llm.turns[1]
    assert followup[1].role == "assistant" and followup[2].role == "user"
    assert "Invalid reply" in followup[2].content
    # Both calls' tokens are attributed to the one generation.
    assert gq.input_tokens == 2 and gq.output_tokens == 2


def test_reask_is_bounded_then_degrades_to_guard() -> None:
    llm = _Recording(["nonsense that never improves"])  # ScriptedLLM repeats the last reply
    gq = _generate(llm)
    assert len(llm.turns) == 3  # initial + 2 re-asks, never more
    # Degrades to the old behavior: the reply goes to sql_guard, which rejects
    # it into the pipeline's repair/escalate path.
    assert gq.sql == "nonsense that never improves"
