"""The typed-decision client: the wire shape it sends, the distribution it
returns verbatim, and the outages it refuses to turn into decisions."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from services.contracts.errors import ProviderError
from services.contracts.protocols import Decider, Option
from services.llm.typesafe import TypesafeDecider

OPTS = [Option("revenue", "money collected"), Option("order_count")]


def _transport(status: int, body: Any, seen: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_one_decision_is_one_choice_question_and_the_distribution_comes_back_verbatim() -> None:
    seen: list[dict[str, Any]] = []
    answer = {
        "model": "jev-latest",
        "answers": {
            "decision": {
                "choice": "revenue",
                "probabilities": {"revenue": 0.91, "order_count": 0.07, "none": 0.02},
                "confidence": 0.9,
            }
        },
        "usage": {"input_tokens": 40, "output_tokens": 0},
    }
    d = TypesafeDecider("k", transport=_transport(200, answer, seen))
    assert isinstance(d, Decider)
    out = d.decide(
        question="total revenue?", context="Which metric?", options=OPTS, allow_none=True
    )
    assert out.chosen == "revenue" and out.provider == "typesafe:jev-latest"
    assert out.probs == {"revenue": 0.91, "order_count": 0.07, "none": 0.02}
    assert out.p == 0.91 and out.margin == pytest.approx(0.84)
    (req,) = seen
    assert req["path"] == "/v1/systemone" and req["auth"] == "Bearer k"
    assert req["body"]["model"] == "jev-latest"
    assert req["body"]["state"] == {"question": "total revenue?"}
    q = req["body"]["questions"]["decision"]
    assert q["type"] == "choice" and q["instructions"] == "Which metric?"
    assert q["criteria"] == {
        "revenue": "money collected",
        "order_count": None,
        "none": "none of the options fits",
    }


def test_none_is_a_none_decision_and_no_none_option_when_not_allowed() -> None:
    seen: list[dict[str, Any]] = []
    answer = {
        "answers": {
            "decision": {
                "choice": "none",
                "probabilities": {"revenue": 0.1, "order_count": 0.1, "none": 0.8},
            }
        }
    }
    d = TypesafeDecider("k", transport=_transport(200, answer, seen))
    out = d.decide(question="q", context="c", options=OPTS, allow_none=True)
    assert out.chosen is None and out.p == 0.8
    d.decide(question="q", context="c", options=OPTS, allow_none=False)
    assert "none" not in seen[1]["body"]["questions"]["decision"]["criteria"]


def test_many_decisions_ride_one_call() -> None:
    seen: list[dict[str, Any]] = []
    answer = {
        "answers": {
            "metric": {"choice": "revenue", "probabilities": {"revenue": 0.9, "order_count": 0.1}},
            "grain": {"choice": "month", "probabilities": {"month": 0.7, "none": 0.3}},
        }
    }
    d = TypesafeDecider("k", transport=_transport(200, answer, seen))
    out = d.decide_many(
        "revenue by month",
        {
            "metric": ("Which metric?", OPTS, False),
            "grain": ("Which period?", [Option("month")], True),
        },
    )
    assert out["metric"].chosen == "revenue" and out["grain"].chosen == "month"
    assert len(seen) == 1 and set(seen[0]["body"]["questions"]) == {"metric", "grain"}


@pytest.mark.parametrize("status", [401, 422, 500])
def test_http_errors_are_provider_errors_never_decisions(status: int) -> None:
    d = TypesafeDecider("k", transport=_transport(status, {"detail": "no"}, []), attempts=1)
    with pytest.raises(ProviderError):
        d.decide(question="q", context="c", options=OPTS, allow_none=True)


def test_an_unknown_option_in_the_answer_is_an_error() -> None:
    answer = {"answers": {"decision": {"choice": "profit", "probabilities": {"profit": 1.0}}}}
    d = TypesafeDecider("k", transport=_transport(200, answer, []))
    with pytest.raises(ProviderError):
        d.decide(question="q", context="c", options=OPTS, allow_none=False)


def test_a_decision_carries_what_it_cost_and_a_batched_call_is_shared() -> None:
    seen: list[dict] = []
    answer = {
        "model": "jev-latest",
        "answers": {
            "a": {"choice": "revenue", "probabilities": {"revenue": 0.9, "order_count": 0.1}},
            "b": {"choice": "none", "probabilities": {"none": 0.8, "revenue": 0.2}},
        },
        "usage": {"input_tokens": 60, "output_tokens": 2},
    }
    d = TypesafeDecider("k", transport=_transport(200, answer, seen))
    out = d.decide_many(
        "total revenue?", {"a": ("Which metric?", OPTS, False), "b": ("Another?", OPTS, True)}
    )
    ua, ub = out["a"].usage, out["b"].usage
    assert ua is not None and ub is not None
    assert ua.calls == 0.5 and ub.calls == 0.5  # one call, two decisions
    assert ua.input_tokens == 30.0 and ua.output_tokens == 1.0
    assert ua.latency_s >= 0.0
    single = {
        "model": "jev-latest",
        "answers": {"decision": {"choice": "revenue", "probabilities": {"revenue": 1.0}}},
        "usage": {"input_tokens": 40, "output_tokens": 0},
    }
    one = TypesafeDecider("k", transport=_transport(200, single, [])).decide(
        question="q", context="c", options=OPTS, allow_none=False
    )
    assert one.usage is not None and one.usage.calls == 1.0 and one.usage.input_tokens == 40.0
