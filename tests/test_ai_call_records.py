"""One usage record per provider call, with its purpose; every LLM call charged.

The defect: a request's AI spend was one total on its request_log row, with no
record of which call it went to; an eval run's spend was recorded nowhere; the
newest models had no price, so their calls read as unpriced; and three LLM
calls on the serving path were never charged to the answer's row: the
certified template's bind gate, the rubric judge the pipeline falls back to
when the typed judge fails, and the opt-in adversary.

Pinned here: every provider call made while serving lands in ai_call with its
purpose, under the id the caller got back (routed declines included), test
traffic is recorded and tagged, an eval case and its run carry their tokens and
cost, the three calls reach the row, and every model the default install can
call has a price, an unknown one named as unpriced.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.certify.store import CertifiedAnswer
from services.config import ProviderConfig, settings
from services.contracts.eval import EvalCase
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import LLMResult
from services.contracts.warehouse import QueryResult
from services.lenses.demo import jaffle_customer_value
from services.llm import registry
from services.observability import cost
from services.observability import usage as ai_usage
from services.runtime import assembly
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query
from tests.test_decider_spend_sites import _ask_judged, _Billing
from tests.test_embedder_degraded import _scripted_generation
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)
IN, OUT = 17, 5


class _Provider:
    """A provider client that reports its usage the way the real ones do."""

    def __init__(self, reply: str, model: str = "claude-haiku-4-5") -> None:
        self.reply, self.model, self.calls = reply, model, 0

    def complete(self, **kw: Any) -> LLMResult:
        self.calls += 1
        ai_usage.record("anthropic", kw.get("model") or self.model, IN, OUT)
        return LLMResult(text=self.reply, input_tokens=IN, output_tokens=OUT)


# ── the three uncharged calls reach the row ────────────────────────────────


def test_the_rubric_judge_fallback_charges_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The typed judge fails; the rubric judge decides instead, and what its
    provider billed is on the row (the typed judge here bills nothing)."""
    judge = _Provider('{"verdict": "approve", "reasoning": "fine"}')

    def ask(llm: Any, fail: bool) -> Any:
        import services.runtime.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: _Billing(fail=fail, bill=False))
        return run_query(
            question="how many customers?",
            lens_name="customer_value",
            org_id="o",
            caller="a",
            semantic_model=jaffle_customer_value(),
            connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[100]])),
            generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
            composer=AnswerComposer(ScriptedLLM(["There are 100 customers."])),
            inline_judge_llm=llm,
        )

    fell_back = ask(judge, fail=True)
    typed = ask(judge, fail=False)
    assert judge.calls == 1 and fell_back.trace.status == "ok"
    extra = (
        (fell_back.trace.ai_input_tokens or 0) - (typed.trace.ai_input_tokens or 0),
        (fell_back.trace.ai_output_tokens or 0) - (typed.trace.ai_output_tokens or 0),
    )
    assert extra == (IN, OUT)
    assert _ask_judged  # the typed-judge arm is pinned in test_decider_spend_sites


def test_the_adversary_charges_the_row() -> None:
    def ask(adversary: Any) -> Any:
        return run_query(
            question="how many customers?",
            lens_name="customer_value",
            org_id="o",
            caller="a",
            semantic_model=jaffle_customer_value(),
            connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[100]])),
            generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
            composer=AnswerComposer(ScriptedLLM(["There are 100 customers."])),
            adversary_llm=adversary,
        )

    challenged = ask(_Provider('{"challenges": []}'))
    plain = ask(None)
    assert (challenged.trace.ai_input_tokens or 0) - (plain.trace.ai_input_tokens or 0) == IN
    assert (challenged.trace.ai_output_tokens or 0) - (plain.trace.ai_output_tokens or 0) == OUT


def _template() -> CertifiedAnswer:
    return CertifiedAnswer(
        "t1",
        "customer_value",
        "how many customers in {region}?",
        "SELECT count(*) FROM customers WHERE region = {region}",
        "t",
        "2026-01-01",
        slots={"region": {"type": "enum", "values": ["north", "south"]}},
    )


@pytest.mark.parametrize("reply", ['{"match": true, "bindings": {"region": "north"}}', "nope"])
def test_the_bind_gate_charges_what_it_billed_whatever_it_decides(reply: str) -> None:
    spent: list[Any] = []
    llm = _Provider(reply)
    bound = assembly.certified_bind(
        llm, "claude-haiku-4-5", "how many customers in north?", _template(), spend_to=spent
    )
    assert (bound is not None) == reply.startswith("{")
    ((provider, usage),) = spent
    assert cost.decision_model(provider) == "claude-haiku-4-5"
    assert (usage.input_tokens, usage.output_tokens) == (IN, OUT)


# ── purposes and nesting ────────────────────────────────────────────────────


def test_a_call_is_recorded_once_per_open_collection_under_its_purpose() -> None:
    llm = _Provider("x")
    with ai_usage.collecting() as outer:
        with ai_usage.purpose("generate"):
            llm.complete(model="claude-sonnet-5-5")
        with ai_usage.collecting() as inner, ai_usage.purpose("bind"):
            llm.complete(model="claude-sonnet-5-5")
    assert [c.purpose for c in outer] == ["generate", "bind"]
    assert [c.purpose for c in inner] == ["bind"]
    assert outer[0].cost_usd == pytest.approx((IN * 2.0 + OUT * 10.0) / 1e6)
    llm.complete(model="claude-sonnet-5-5")  # none open: nothing kept, nothing raised


# ── prices ──────────────────────────────────────────────────────────────────


def test_every_model_the_default_install_can_call_has_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "providers",
        {
            "claude": ProviderConfig(type="anthropic", api_key="k"),
            "decisions": ProviderConfig(type="openai-decisions", api_key="k"),
        },
    )
    assert registry.unpriced_note() is None
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "gpt-5.5"):
        assert cost.price_of(model) is not None, model
    # A call that reads and writes the prompt cache is priced too, for every
    # model this install can call: a cache token with no rate leaves the call
    # unpriced, so a model missing one is a floor on every total.
    for model in registry.resolvable_models():
        assert (
            cost.ai_cost_usd(
                model,
                IN,
                OUT,
                cache_read_tokens=IN,
                cache_write_tokens=IN,
                cache_write_long_tokens=1,
                endpoint="openai-decisions" if model == "gpt-6-luna" else None,
            )
            is not None
        ), f"{model}: a cached call is unpriced"
    # The anthropic client marks its system prompt cacheable: every Claude
    # model the list prices has every cache rate.
    claude = [m for m in cost._BUILTIN_AI_PRICES if m.startswith("claude-")]
    missing = [m for m in claude if None in (cost.cache_price_of(m) or (None,))]
    assert not missing, f"Claude models with no full set of cache rates: {missing}"
    assert cost.price_of("claude-sonnet-5") == (2.0, 10.0)  # the launch rate is standard now
    assert cost.price_of("claude-haiku-4-5-20251001") == (1.0, 5.0)  # a snapshot id


def test_an_unknown_model_is_named_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "providers",
        {
            "gw": ProviderConfig(
                type="openai-compatible",
                api_key="k",
                base_url="http://gw",
                fast_model="house-model-7",
            )
        },
    )
    note = registry.unpriced_note() or ""
    assert "house-model-7" in note and "UNPRICED" in note.upper() and "ai_pricing" in note
    assert cost.ai_cost_usd("house-model-7", 10, 10) is None
    assert client.get("/ready").json()["ai_pricing"].startswith("UNPRICED (")


def test_a_long_prompt_is_priced_at_its_long_rate() -> None:
    short = cost.ai_cost_usd("claude-haiku-5-5", 100_000, 1_000)
    long = cost.ai_cost_usd("claude-haiku-5-5", 100_001, 1_000)
    assert short == pytest.approx(100_000 * 0.10 / 1e6 + 1_000 * 0.50 / 1e6, abs=1e-6)
    assert long == pytest.approx(100_001 * 0.50 / 1e6 + 1_000 * 2.50 / 1e6, abs=1e-6)


# ── evals carry their spend ─────────────────────────────────────────────────


def test_an_eval_case_carries_what_its_calls_cost() -> None:
    from services.evals.runner import run_behavioral

    llm = _Provider("x")

    def serve(question: str) -> Any:
        with ai_usage.purpose("generate"):
            llm.complete(model="claude-haiku-4-5")
        return run_query(
            question=question,
            lens_name="customer_value",
            org_id="o",
            caller="a",
            semantic_model=jaffle_customer_value(),
            connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[100]])),
            generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
            composer=None,
        )

    case = EvalCase(
        id="c1",
        lens="customer_value",
        question="how many customers?",
        source="authored",
        expect="answer",
    )
    (result,) = run_behavioral(lens="customer_value", cases=[case], serve=serve).results
    assert (result.ai_input_tokens, result.ai_output_tokens) == (IN, OUT)
    assert result.ai_cost_usd == cost.ai_cost_usd("claude-haiku-4-5", IN, OUT)


@needs_db
def test_an_eval_run_records_its_cases_spend() -> None:
    from services.contracts.eval import EvalResult
    from services.db.session import org_session
    from services.evals import store

    org, _raw = _make_org_token(settings.database_admin_url)
    try:
        with org_session(org) as session:
            run_id = store.create_run(session, "customer_value", "test")
            store.record_results(
                session,
                run_id,
                [
                    EvalResult(
                        run_id=run_id,
                        case_id="a",
                        passed=True,
                        ai_input_tokens=10,
                        ai_output_tokens=2,
                        ai_cost_usd=0.001,
                    ),
                    EvalResult(
                        run_id=run_id,
                        case_id="b",
                        passed=True,
                        ai_input_tokens=5,
                        ai_output_tokens=1,
                        ai_cost_usd=None,
                    ),
                ],
            )
            run = session.execute(
                text(
                    "SELECT ai_input_tokens, ai_output_tokens, ai_cost_usd FROM eval_run "
                    "WHERE id = :r"
                ),
                {"r": run_id},
            ).one()
            rows = session.execute(
                text(
                    "SELECT case_id, ai_cost_usd FROM eval_result WHERE run_id = :r "
                    "ORDER BY case_id"
                ),
                {"r": run_id},
            ).all()
        assert tuple(run) == (15, 3, None)  # one unpriced case: the run is unpriced
        assert [(r[0], r[1]) for r in rows] == [("a", 0.001), ("b", None)]
    finally:
        _cleanup(settings.database_admin_url, org)


# ── the doors write one row per call ────────────────────────────────────────


def _ai_calls(org: object) -> list[tuple[Any, ...]]:
    with create_engine(settings.database_admin_url).begin() as c:
        return [
            tuple(r)
            for r in c.execute(
                text(
                    "SELECT request_id, purpose, provider, input_tokens, agent FROM ai_call "
                    "WHERE org_id = :o ORDER BY id"
                ),
                {"o": org},
            )
        ]


@needs_db
def test_a_served_answer_writes_one_row_per_call_with_its_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.db.session import org_session
    from services.observability import observe

    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    _scripted_generation(monkeypatch)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    try:
        headers = {"Authorization": f"Bearer {raw}"}
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        rid = r.json()["request_id"]
        calls = _ai_calls(org)
        assert {(c[0], c[1]) for c in calls} == {(rid, "generate"), (rid, "compose")}
        # Test traffic: recorded and tagged, left out of the purposes unless asked.
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={**headers, "X-Dst-Agent": "dst-test"},
        )
        assert r.status_code == 200, r.text
        assert sum(1 for c in _ai_calls(org) if c[4] == "dst-test") == 2
        with org_session(org) as s:
            served = observe.kpis(s)["ai_by_purpose"]
            everything = observe.kpis(s, include_test=True)["ai_by_purpose"]
        assert served["generate"]["calls"] == 1 and everything["generate"]["calls"] == 2
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_a_routed_decline_writes_the_routing_call_under_the_id_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.router import RouteDecision

    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)

    def route(caller: Any, q: str) -> RouteDecision:
        with ai_usage.purpose("route"):
            _Provider("none").complete(model="claude-haiku-4-5")
        return RouteDecision(False, None, 0.2, [], "no lens covers this")

    monkeypatch.setattr("services.api.route._route", route)
    monkeypatch.setattr("services.api.route.lens_refusal_named", lambda q, caller: None)
    monkeypatch.setattr("services.api.route._not_for_named", lambda q, caller, lens: None)
    try:
        r = client.post(
            "/v1/query", json={"q": "weather tomorrow?"}, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        assert _ai_calls(org) == [(r.json()["request_id"], "route", "anthropic", IN, None)]
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        _cleanup(settings.database_admin_url, org)
