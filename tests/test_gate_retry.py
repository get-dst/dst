"""Retry-before-fail in the publish gate.

One generation per case with block-on-any-failure made a green apply
statistically unreachable — (1-p)^N over a ~120-case suite with generation's
own wobble as p, the exact one-rep verdict dst-flywheel warns against. A
gate case now fails only after failing every one of GATE_ATTEMPTS attempts;
retries are paid only on failures; a pass-on-retry is surfaced as FLAKY (a
different fact from a clean pass, and from a real failure).
"""

from __future__ import annotations

from typing import Any

from services.contracts.eval import EvalCase
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.evals.runner import FLAKY_PREFIX, GATE_ATTEMPTS, run_behavioral
from services.evals.service import gate_decision, gate_decision_dict
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs


class _FlakyGenerator:
    """Fails (out-of-scope SQL → a served answer becomes a rejection) for the
    first *fail_first* attempts, then emits good SQL — deterministic wobble."""

    model = "flaky"

    def __init__(self, good_sql: str, bad_sql: str, fail_first: int) -> None:
        self.calls = 0
        self._good, self._bad, self._fail_first = good_sql, bad_sql, fail_first

    def generate(self, **_kw: Any) -> GeneratedQuery:
        self.calls += 1
        sql = self._bad if self.calls <= self._fail_first else self._good
        return GeneratedQuery(sql=sql)


def _model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="c", table="orders"),
                fields=[Field(name="n", type="number")],
            )
        ],
    )


_GOOD = "SELECT SUM(orders.n) AS total FROM orders AS orders"
_BAD = "SELECT secret FROM raw_secrets"  # out of scope → rejected, never served


def _run(fail_first: int):
    model = _model()
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
    )
    gen = _FlakyGenerator(_GOOD, _BAD, fail_first)
    case = EvalCase(id="c1", lens="t", question="total orders?", source="authored", expect="answer")
    outcome = run_behavioral(
        connector=FakeConnector(result=QueryResult(columns=["total"], rows=[[7]])),
        lens="t",
        cases=[case],
        assemble_for=lambda _q: assembled,
        generators_for=lambda _a: (gen, None),
        composer=AnswerComposer(ScriptedLLM(["Seven."])),
    )
    return outcome, gen


def test_a_case_failing_once_then_passing_does_not_fail_the_gate() -> None:
    # fail_first=2 outlasts attempt 1's in-pipeline repair (initial + 1 repair,
    # both bad), so attempt 1 genuinely fails and attempt 2 passes — the
    # retry's job, distinct from the repair loop's.
    outcome, gen = _run(fail_first=2)
    (r,) = outcome.results
    assert r.passed, r.reason
    assert (r.reason or "").startswith(FLAKY_PREFIX)  # wobble reads as wobble
    assert gen.calls >= 3  # attempt 1 (2 calls) + attempt 2


def test_a_consistently_failing_case_still_fails_after_every_attempt() -> None:
    outcome, gen = _run(fail_first=99)
    (r,) = outcome.results
    assert not r.passed
    assert gen.calls >= GATE_ATTEMPTS  # every attempt was spent before failing


def test_a_clean_pass_costs_exactly_one_attempt() -> None:
    outcome, gen = _run(fail_first=0)
    (r,) = outcome.results
    assert r.passed and r.reason is None  # no flaky annotation on a clean pass
    assert gen.calls == 1  # retries are paid only on failures


def test_flaky_cases_ride_the_gate_decision() -> None:
    d = gate_decision(gate="block", score=1.0, prev_score=None, failing=[])
    d.flaky = ["c1"]
    assert not d.blocked  # flaky never blocks
    dumped = gate_decision_dict(d)
    assert dumped is not None and dumped["flaky"] == ["c1"]
