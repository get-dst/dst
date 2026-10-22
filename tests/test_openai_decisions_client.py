"""OpenAI's Decisions API on the decision seam: the wire shape the client sends,
the probabilities it keeps as returned, refusals that decline instead of
picking, outages that stay loud, and what each decision cost.

Every response here follows the documented shapes (choice, refusal, a batch of
questions by name); a live call is the opt-in lane at the bottom."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from services.contracts.errors import ProviderError
from services.contracts.protocols import Decider, Decision, Option
from services.llm import openai_decisions
from services.llm.openai_decisions import OpenAIDecisionsDecider
from services.observability import cost
from services.runtime import decision_policy as dp

OPTS = [Option("tonnage", "cargo weight landed"), Option("berth_hours")]


def _transport(
    replies: list[tuple[int, Any]] | tuple[int, Any], seen: list[dict[str, Any]]
) -> httpx.MockTransport:
    queue = list(replies) if isinstance(replies, list) else None

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        status, body = queue.pop(0) if queue is not None else replies  # type: ignore[misc]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _choice(name: str, choice: str, probs: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "choice",
        "name": name,
        "choice": choice,
        "probabilities": [{"value": v, "probability": p} for v, p in probs.items()],
        "confidence": 0.9,
    }


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_decisions.time, "sleep", lambda _s: None)


def test_one_decision_is_one_choice_question_and_probabilities_come_back_as_returned() -> None:
    seen: list[dict[str, Any]] = []
    # Overconfident on the top option, as community reports describe: kept as is.
    body = {
        "answers": [
            _choice("q0", "tonnage", {"tonnage": 0.99, "berth_hours": 0.004, "none": 0.001})
        ],
        "usage": {"input_tokens": 52},
    }
    d = OpenAIDecisionsDecider("sk-test", transport=_transport((200, body), seen))
    assert isinstance(d, Decider) and d.typed_decisions
    out = d.decide(
        question="tonnage landed in May?", context="Which metric?", options=OPTS, allow_none=True
    )
    assert out.chosen == "tonnage" and out.provider == "openai-decisions:gpt-6-luna"
    assert out.probs == {"tonnage": 0.99, "berth_hours": 0.004, "none": 0.001}
    assert out.refused is False
    (req,) = seen
    assert req["url"] == "https://api.openai.com/v1/decisions" and req["auth"] == "Bearer sk-test"
    assert req["body"] == {
        "model": "gpt-6-luna",
        "input": "tonnage landed in May?",
        "questions": [
            {
                "type": "choice",
                "name": "q0",
                "instructions": "Which metric?",
                "choices": [
                    {"value": "tonnage", "description": "cargo weight landed"},
                    {"value": "berth_hours"},
                    {"value": "none", "description": "none of the options fits"},
                ],
            }
        ],
    }
    # The policy is the typed regime's: the choice acts, whatever its p.
    assert dp.verdict(out, "metric") == "act"
    assert dp.policy_for("metric", out.provider) == dp.ARGMAX
    assert dp.disclose_below("lens", out.provider) is None  # no run measured a bar


def test_none_is_a_none_decision_and_no_none_choice_when_not_allowed() -> None:
    seen: list[dict[str, Any]] = []
    body = {"answers": [_choice("q0", "none", {"tonnage": 0.1, "berth_hours": 0.1, "none": 0.8})]}
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), seen))
    out = d.decide(question="q", context="c", options=OPTS, allow_none=True)
    assert out.chosen is None and out.p == 0.8 and dp.verdict(out, "metric") == "decline"
    body["answers"] = [_choice("q0", "tonnage", {"tonnage": 0.6, "berth_hours": 0.4})]
    d.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert [c["value"] for c in seen[1]["body"]["questions"][0]["choices"]] == [
        "tonnage",
        "berth_hours",
    ]


def test_a_refusal_is_a_declined_none_never_a_pick() -> None:
    body = {"answers": [{"type": "refusal", "name": "q0"}], "usage": {"input_tokens": 30}}
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), []))
    out = d.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert out.refused and out.chosen is None and out.probs is None
    assert dp.verdict(out, "metric") == "decline"
    assert dp.describe(out, 2) == "none (refused, openai-decisions:gpt-6-luna)"
    assert out.usage is not None and out.usage.input_tokens == 30.0


def test_many_decisions_ride_one_call_by_question_name() -> None:
    seen: list[dict[str, Any]] = []
    body = {
        # Out of order on the wire: matched by name, not position.
        "answers": [
            {"type": "refusal", "name": "q2"},
            _choice("q1", "month", {"month": 0.7, "none": 0.3}),
            _choice("q0", "tonnage", {"tonnage": 0.9, "berth_hours": 0.1}),
        ],
        "usage": {"input_tokens": 90, "output_tokens": 0},
    }
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), seen))
    out = d.decide_many(
        "tonnage by month",
        {
            "metric:0": ("Which metric?", OPTS, False),
            "grain": ("Which period?", [Option("month")], True),
            "shape": ("Which shape?", [Option("figure"), Option("listing")], True),
        },
    )
    assert out["metric:0"].chosen == "tonnage" and out["grain"].chosen == "month"
    assert out["shape"].refused and out["shape"].chosen is None
    assert len(seen) == 1
    assert [q["name"] for q in seen[0]["body"]["questions"]] == ["q0", "q1", "q2"]
    # One call, three decisions: each carries a third of it.
    use = out["grain"].usage
    assert use is not None and use.calls == pytest.approx(1 / 3)
    assert use.input_tokens == pytest.approx(30.0) and use.output_tokens == 0.0


def test_a_missing_or_unknown_answer_is_an_error_not_a_decision() -> None:
    d = OpenAIDecisionsDecider("k", transport=_transport((200, {"answers": []}), []))
    with pytest.raises(ProviderError, match="missing answer"):
        d.decide(question="q", context="c", options=OPTS, allow_none=False)
    body = {"answers": [_choice("q0", "draught", {"draught": 1.0})]}
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), []))
    with pytest.raises(ProviderError, match="unknown option"):
        d.decide(question="q", context="c", options=OPTS, allow_none=False)
    body = {"answers": [{"type": "score", "name": "q0", "score": 1.0}]}
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), []))
    with pytest.raises(ProviderError, match="malformed answer"):
        d.decide(question="q", context="c", options=OPTS, allow_none=False)


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_client_errors_are_provider_errors_at_once(status: int) -> None:
    seen: list[dict[str, Any]] = []
    d = OpenAIDecisionsDecider(
        "k", transport=_transport((status, {"error": {"message": "no"}}), seen)
    )
    with pytest.raises(ProviderError) as exc:
        d.decide(question="q", context="c", options=OPTS, allow_none=True)
    assert exc.value.status == status and exc.value.provider == "openai-decisions"
    assert len(seen) == 1  # never retried


@pytest.mark.parametrize("status", [429, 500, 503])
def test_throttles_and_server_errors_retry_then_fail_loud(status: int) -> None:
    seen: list[dict[str, Any]] = []
    ok = {"answers": [_choice("q0", "tonnage", {"tonnage": 1.0})]}
    d = OpenAIDecisionsDecider("k", transport=_transport([(status, {}), (200, ok)], seen))
    assert d.decide(question="q", context="c", options=OPTS, allow_none=False).chosen == "tonnage"
    assert len(seen) == 2
    seen.clear()
    down = OpenAIDecisionsDecider("k", transport=_transport((status, {}), seen), attempts=3)
    with pytest.raises(ProviderError) as exc:
        down.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert exc.value.status == status and len(seen) == 3


def test_a_timeout_is_a_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    d = OpenAIDecisionsDecider("k", transport=httpx.MockTransport(handler), attempts=2)
    with pytest.raises(ProviderError, match="timed out"):
        d.decide(question="q", context="c", options=OPTS, allow_none=False)


def test_usage_is_priced_input_only_and_a_gateway_base_url_is_honoured() -> None:
    seen: list[dict[str, Any]] = []
    body = {
        "answers": [_choice("q0", "tonnage", {"tonnage": 1.0})],
        "usage": {"input_tokens": 200_000, "output_tokens": 7},
    }
    d = OpenAIDecisionsDecider(
        "k",
        "https://gateway.example/openai/v1/",
        model="gpt-6-luna",
        transport=_transport((200, body), seen),
    )
    out = d.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert seen[0]["url"] == "https://gateway.example/openai/v1/decisions"
    assert out.usage is not None and out.usage.calls == 1.0 and out.usage.latency_s >= 0.0
    assert cost.decision_model(out.provider) == "gpt-6-luna"
    # $0.10 per 1M input tokens; output is not billed.
    spent = cost.decisions_cost_usd(
        {out.provider: (out.usage.input_tokens, out.usage.output_tokens)}
    )
    assert spent == 0.02


def test_a_response_without_usage_is_said_once(caplog: pytest.LogCaptureFixture) -> None:
    body = {"answers": [_choice("q0", "tonnage", {"tonnage": 1.0})]}
    d = OpenAIDecisionsDecider("k", transport=_transport((200, body), []))
    with caplog.at_level("WARNING", logger="dst"):
        for _ in range(2):
            out = d.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert out.usage is not None and out.usage.input_tokens == 0.0
    assert sum("carried no usage" in r.message for r in caplog.records) == 1


def test_a_refusal_declines_the_question_naming_it_and_nothing_is_picked() -> None:
    """Through the typed resolver: a provider that refuses leaves no acted
    slot. The question declines with the refusal named, and the decision on
    the resolution is recorded as refused, never as a confident `none` (on an
    optional slot that would drop a filter without a word)."""
    from services.lenses.demo import jaffle_customer_value
    from services.runtime.typed_resolver import TypedResolver
    from tests.test_typed_provider_down import QUESTION, _ask

    def handler(request: httpx.Request) -> httpx.Response:
        names = [q["name"] for q in json.loads(request.content)["questions"]]
        answers = [{"type": "refusal", "name": n} for n in names]
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 90}})

    decider = OpenAIDecisionsDecider("k", transport=httpx.MockTransport(handler))
    res = TypedResolver(decider).resolve(QUESTION, jaffle_customer_value())
    assert res.intent is None and res.clarification is None
    assert res.decline is not None and "refused" in res.decline
    assert res.decisions and all(
        r.refused and r.chosen is None and r.verdict == "decline" for r in res.decisions
    )
    served = _ask(decider, fallback=False)
    assert served.response.status == "refused" and served.response.data is None
    assert "openai-decisions:gpt-6-luna) refused to decide" in served.response.answer


def test_a_refused_route_names_the_refusal_not_coverage() -> None:
    from services.contracts.fakes import HashEmbedder
    from services.router import CoverageProfile, Router
    from services.router.decider import LlmDecider, route_with_llm

    profiles = [
        CoverageProfile(lens="harbour", anchors=["tonnage landed"], description="cargo"),
        CoverageProfile(lens="crew", anchors=["shift hours"], description="rosters"),
    ]
    body = {"answers": [{"type": "refusal", "name": "q0"}]}
    oad = OpenAIDecisionsDecider("k", transport=_transport((200, body), []))
    decider = LlmDecider(None, "", decider=oad)
    route = route_with_llm(
        Router(profiles, HashEmbedder()), {p.lens: p for p in profiles}, decider, "tonnage?"
    )
    assert not route.covered and route.lens is None
    assert (
        route.reason == "the decision provider (openai-decisions:gpt-6-luna) refused to route this"
    )


def test_an_outage_is_named_on_the_refusal() -> None:
    from tests.test_typed_provider_down import _ask

    down = OpenAIDecisionsDecider(
        "k", transport=httpx.MockTransport(lambda r: httpx.Response(402, json={})), attempts=1
    )
    res = _ask(down, fallback=False)
    assert res.response.status == "refused"
    assert "typed decisions unavailable: openai-decisions HTTP 402" in res.response.answer


# ── live lane ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("DST_TEST_OPENAI_DECISIONS") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="live Decisions API lane: set DST_TEST_OPENAI_DECISIONS=1 and OPENAI_API_KEY",
)
def test_live_decisions_api_round_trip() -> None:
    """One real batch: a choice, a none-eligible choice, the usage read back.
    Confirms what the mocks above assume about the wire."""
    d = OpenAIDecisionsDecider(os.environ["OPENAI_API_KEY"])
    out = d.decide_many(
        "How many tonnes of cargo did the harbour land in May?",
        {
            "metric": ("Which metric does the question ask for?", OPTS, False),
            "colour": (
                "Which hull colour does the question ask about?",
                [Option("red"), Option("blue")],
                True,
            ),
        },
    )
    metric = out["metric"]
    assert isinstance(metric, Decision) and metric.chosen == "tonnage"
    assert metric.probs and set(metric.probs) <= {"tonnage", "berth_hours"}
    assert out["colour"].chosen is None  # none, or a refusal
    assert metric.usage is not None and metric.usage.input_tokens > 0
