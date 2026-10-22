"""Every decider call site charges the answer's row what its provider billed.

A decision's tokens reach the request row (and from it observe and the eval
cost) only when its record carries ``usage`` and when a failed call's spend
(``contracts.protocols.spent_by``) is charged by whoever catches it. A record
built by hand once dropped ``usage`` at the typed judge, the reading decision
and the certified-equivalence gate, and two catch-all handlers dropped the
spend of a failed call. Pinned here, three ways:

- each site, driven end to end with a provider that bills, charges the row
  exactly what was billed, on success and when the provider fails after
  billing;
- every record that carries a measurement carries its cost (static);
- every place a decider is called, or a decision-making wrapper is used, is
  listed below with the test that proves its spend reaches a row, or the
  reason no row exists; a new one fails until it is listed (static).
"""

from __future__ import annotations

import ast
import contextlib
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.certify import store as certify_store
from services.contracts.errors import ProviderError
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.lens_config import LensConfig, ModelConfig
from services.contracts.protocols import Decision, DecisionUsage, Option, spend_on
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import QueryResult
from services.lenses.demo import jaffle_customer_value
from services.lenses.store import LensBundle
from services.runtime import assembly, typed_health
from services.runtime import pipeline as pipeline_mod
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.typed_resolver import TypedResolver
from tests.test_typed_reading import NET, QUESTION
from tests.test_typed_reading import _model as _reading_model
from tests.test_typed_reading import _run as _ask_reading

ROOT = Path(__file__).resolve().parents[1]
IN, OUT = 13, 3  # what one billed call costs: never a round number


class _Billing:
    """A decider whose provider bills every call ``IN``/``OUT`` tokens. It
    picks *pick* when offered, else the first option, at p=1. ``fail``: the
    call is billed and then fails, its spend riding the failure as a vote
    decider's or a typed client's does. ``bill=False`` is the free arm the
    billed one is measured against."""

    provider = "billing"

    def __init__(self, pick: str | None = None, *, fail: bool = False, bill: bool = True) -> None:
        self._pick, self._fail, self._bill = pick, fail, bill
        self.calls = 0
        self.billed = (0, 0)

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.calls += 1
        usage = DecisionUsage(calls=1, input_tokens=IN, output_tokens=OUT) if self._bill else None
        if usage is not None:
            self.billed = (self.billed[0] + IN, self.billed[1] + OUT)
        if self._fail:
            exc = ProviderError(self.provider, "upstream unavailable", status=503)
            if usage is not None:
                spend_on(exc, self.provider, usage)
            raise exc
        names = [o.name for o in options]
        chosen = self._pick if self._pick in names else names[0]
        return Decision(chosen=chosen, probs={chosen: 1.0}, provider=self.provider, usage=usage)


def _row(res: Any) -> tuple[int, int]:
    return res.trace.ai_input_tokens or 0, res.trace.ai_output_tokens or 0


def _charged(billed: Any, free: Any) -> tuple[int, int]:
    """What the billed arm's row carries beyond the free arm's: the decider."""
    (bi, bo), (fi, fo) = _row(billed), _row(free)
    return bi - fi, bo - fo


@pytest.fixture(autouse=True)
def _healthy() -> Any:
    yield
    typed_health.reset()


# ── the typed judge (pipeline, inline) ───────────────────────────────────────


def _ask_judged(decider: _Billing, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: decider)
    return run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[100]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(ScriptedLLM(["There are 100 customers."])),
        inline_judge_llm=ScriptedLLM(['{"verdict": "approve", "reasoning": "fine"}']),
    )


@pytest.mark.parametrize("fail", [False, True], ids=["decided", "failed"])
def test_the_typed_judge_charges_the_row(fail: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    billed = _Billing("within_scope", fail=fail)
    res = _ask_judged(billed, monkeypatch)
    free = _ask_judged(_Billing("within_scope", fail=fail, bill=False), monkeypatch)
    assert billed.calls == 1 and res.trace.status == "ok", res.trace.error
    assert _charged(res, free) == billed.billed == (IN, OUT)


# ── the reading decision (pipeline and resolver) ─────────────────────────────


@pytest.mark.parametrize("fail", [False, True], ids=["decided", "failed"])
def test_the_reading_decision_charges_the_row(fail: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    billed = _Billing(NET, fail=fail)
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: billed)
    res = _ask_reading(QUESTION)
    typed_health.reset()
    free_decider = _Billing(NET, fail=fail, bill=False)
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: free_decider)
    free = _ask_reading(QUESTION)
    assert billed.calls == 1
    assert res.trace.status == ("clarification" if fail else "ok"), res.trace.error
    assert _charged(res, free) == billed.billed == (IN, OUT)


def test_the_resolvers_reading_decision_carries_its_cost() -> None:
    billed = _Billing(NET)
    res = TypedResolver(billed).resolve(QUESTION, _reading_model())  # type: ignore[arg-type]
    assert any(r.slot == "reading" for r in res.decisions)
    records = [*res.decisions, *res.unused]
    spent = (
        sum(r.usage["input_tokens"] for r in records if r.usage),
        sum(r.usage["output_tokens"] for r in records if r.usage),
    )
    assert spent == billed.billed


# ── the certified-equivalence gate (assembly → pipeline) ─────────────────────


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = "How many orders shipped?"
    sql: str = "SELECT count(*) AS n FROM orders WHERE status = 'shipped'"
    slots: dict[str, str] | None = None
    sample_bindings: list[dict[str, str]] | None = None
    source: str | None = None
    resolution: dict[str, object] | None = None
    terms: list[str] | None = None


@dataclass
class _Hit:
    answer: _Answer = field(default_factory=_Answer)
    score: float = 0.0


class _MeteredLLM:
    """The cheap model behind the gate: answers *reply*, bills IN/OUT."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.billed = (0, 0)

    def complete(self, **_kw: Any) -> Any:
        from services.contracts.protocols import LLMResult

        self.billed = (self.billed[0] + IN, self.billed[1] + OUT)
        return LLMResult(text=self._reply, input_tokens=IN, output_tokens=OUT)


def _assemble_gate(monkeypatch: pytest.MonkeyPatch, llm: Any) -> Any:
    @contextlib.contextmanager
    def _no_session(_org: Any) -> Any:
        yield None

    bands = assembly.certified_bands()
    hit = _Hit(score=(bands.equiv + bands.exact) / 2)  # the band the gate decides
    monkeypatch.setattr(assembly, "embed_question_degraded", lambda q: ([0.1], None))
    monkeypatch.setattr(assembly, "profile_facts", lambda b, o: ([], None, None, {}))
    monkeypatch.setattr(assembly, "org_session", _no_session)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [hit])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=llm, name="fast"))
    bundle = LensBundle(
        config=LensConfig(name="orders", display_name="Orders", model=ModelConfig()),
        semantic_model=SemanticModel(lens="orders", dialect="duckdb"),
    )
    return assembly.assemble(bundle, "how many orders did we ship?", "org-1")


def _serve(assembled: Any) -> Any:
    return run_query(
        question="how many orders did we ship?",
        lens_name="orders",
        org_id="org-1",
        caller="a",
        semantic_model=assembled.model,
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[4]])),
        generator=FixedSQLGenerator("SELECT 4 AS n"),
        composer=None,
        decisions=assembled.decisions,
        spent=assembled.spent,
    )


def test_the_equivalence_gate_charges_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _MeteredLLM("yes")
    assembled = _assemble_gate(monkeypatch, llm)
    assert assembled.certification == "certified" and assembled.certified_match == "equivalent"
    (record,) = assembled.decisions
    assert record.slot == "certified_equivalent" and record.usage is not None
    free = _serve(SimpleNamespace(model=assembled.model, decisions=[], spent=[]))
    assert _charged(_serve(assembled), free) == llm.billed == (IN, OUT)


def test_a_failed_equivalence_gate_charges_what_it_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def billed_then_failed(*_a: Any, **_kw: Any) -> Decision:
        exc = ProviderError("legacy:fast", "reply cut off", status=502)
        spend_on(exc, "legacy:fast", DecisionUsage(calls=1, input_tokens=IN, output_tokens=OUT))
        raise exc

    monkeypatch.setattr(assembly, "equivalence_decision", billed_then_failed)
    assembled = _assemble_gate(monkeypatch, _MeteredLLM("yes"))
    assert assembled.certification != "certified" and assembled.decisions == []  # fails closed
    free = _serve(SimpleNamespace(model=assembled.model, decisions=[], spent=[]))
    assert _charged(_serve(assembled), free) == (IN, OUT)


# ── static: a measured record carries its cost ───────────────────────────────

_MEASURED = {"p", "runner_up", "margin", "votes", "k", "p_lower"}


def _services() -> list[Path]:
    return sorted((ROOT / "services").rglob("*.py"))


def test_every_measured_record_carries_its_usage() -> None:
    """A ``DecisionRecord`` that carries a measurement (p, margin, votes) or a
    provider read off a decision is a decision a provider was paid for; it
    names ``usage`` or its cost never reaches the row. Deterministic records
    (the question's own text, a binding) measure nothing and cost nothing."""
    missing = []
    for path in _services():
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DecisionRecord"
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            provider = kw.get("provider")
            measured = bool(_MEASURED & kw.keys()) or (
                isinstance(provider, ast.Attribute) and provider.attr == "provider"
            )
            if measured and "usage" not in kw:
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not missing, (
        "DecisionRecord built from a decision without usage (use "
        f"decision_policy.record): {missing}"
    )


def test_assemblys_failed_spend_rides_with_its_decisions() -> None:
    """Assembly hands the pipeline its decisions and what a failed one billed
    together: a serve that passes the one without the other drops a cost."""
    lone = []
    for path in _services():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "run_query"
                and {k.arg for k in node.keywords} & {"decisions", "spent"}
                and not {"decisions", "spent"} <= {k.arg for k in node.keywords}
            ):
                lone.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not lone, f"run_query given decisions without spent (or the reverse): {lone}"


# ── static: every decider call site is accounted for ─────────────────────────

# Callee names: a decider's own entry points, and the functions that make a
# decision for their caller (the caller then owns charging it).
_CALLEES = {
    "decide",
    "decide_many",
    "judge_typed",
    "decide_reading",
    "equivalence_decision",
    "certified_equivalent",
    "route_with_llm",
}
_HERE = "tests/test_decider_spend_sites.py"
_SPEND = "tests/test_decision_spend.py"
_API = "tests/test_query_api.py"
# site -> the test proving its spend reaches a row, or "no row: why".
SITES: dict[str, str] = {
    "services/llm/openai_decisions.py::OpenAIDecisionsDecider.decide -> decide_many": (
        f"{_SPEND}::test_an_unreadable_typed_reply_carries_the_call_it_billed"
    ),
    "services/reviews/judge.py::judge_typed -> decide": (
        f"{_HERE}::test_the_typed_judge_charges_the_row"
    ),
    "services/runtime/pipeline.py::run_query -> judge_typed": (
        f"{_HERE}::test_the_typed_judge_charges_the_row"
    ),
    "services/reviews/service.py::open_review -> judge_typed": (
        "no row: a review ticket records the judge's verdict, and tickets carry no cost"
    ),
    "services/runtime/reading.py::decide_reading -> decide": (
        f"{_HERE}::test_the_reading_decision_charges_the_row"
    ),
    "services/runtime/pipeline.py::run_query -> decide_reading": (
        f"{_HERE}::test_the_reading_decision_charges_the_row"
    ),
    "services/runtime/typed_resolver.py::TypedResolver._readings -> decide_reading": (
        f"{_HERE}::test_the_resolvers_reading_decision_carries_its_cost"
    ),
    "services/runtime/typed_resolver.py::TypedResolver._decide -> decide": (
        f"{_SPEND}::test_the_answer_row_carries_what_a_failed_resolution_billed"
    ),
    "services/runtime/typed_resolver.py::TypedResolver._fan_out -> decide_many": (
        f"{_SPEND}::test_the_answer_row_carries_the_whole_call"
    ),
    "services/runtime/assembly.py::equivalence_decision -> decide": (
        f"{_HERE}::test_the_equivalence_gate_charges_the_row"
    ),
    "services/runtime/assembly.py::certified_equivalent -> equivalence_decision": (
        f"{_HERE}::test_a_failed_equivalence_gate_charges_what_it_billed"
    ),
    "services/runtime/assembly.py::certified_lookup -> certified_equivalent": (
        f"{_HERE}::test_the_equivalence_gate_charges_the_row"
    ),
    "services/router/decider.py::LlmDecider.decide_measured -> decide": (
        f"{_API}::test_routing_spend_on_a_served_answer_is_counted_once"
    ),
    "services/router/decider.py::route_with_llm -> decide": (
        f"{_SPEND}::test_a_routing_outage_carries_the_votes_that_answered"
    ),
    "services/router/decider.py::route_question -> route_with_llm": (
        f"{_API}::test_a_routing_outage_records_the_votes_it_was_billed_for"
    ),
    "services/evals/calibrate.py::lens_decisions.one -> route_with_llm": (
        "no row: calibration grades the decisions and reports no cost"
    ),
    "services/evals/calibrate.py::equivalence_decisions.one -> equivalence_decision": (
        "no row: calibration grades the decisions and reports no cost"
    ),
}


class _Sites(ast.NodeVisitor):
    """``path::Scope.function -> callee`` for every call to one of _CALLEES."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.found: set[str] = set()

    def _scoped(self, node: Any) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name in _CALLEES:
            self.found.add(f"{self.rel}::{'.'.join(self.scope)} -> {name}")
        self.generic_visit(node)


def _call_sites() -> set[str]:
    found: set[str] = set()
    for path in _services():
        visit = _Sites(str(path.relative_to(ROOT)))
        visit.visit(ast.parse(path.read_text()))
        found |= visit.found
    return found


def test_every_decider_call_site_is_accounted_for() -> None:
    sites = _call_sites()
    new = sorted(sites - SITES.keys())
    assert not new, (
        "a new place makes a decision: prove its billed usage reaches the answer's "
        f"row (success and failure) and list it in SITES: {new}"
    )
    gone = sorted(SITES.keys() - sites)
    assert not gone, f"SITES lists call sites that no longer exist: {gone}"


def test_every_listed_proof_exists() -> None:
    for site, proof in SITES.items():
        if proof.startswith("no row: "):
            continue
        module, _, name = proof.partition("::")
        mod = importlib.import_module(module.removesuffix(".py").replace("/", "."))
        assert callable(getattr(mod, name, None)), f"{site}: {proof} is not a test"
