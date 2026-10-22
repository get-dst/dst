"""Every token a batched decision call billed lands on the answer's row once.

A batching decider (``decide_many``) answers a round's independent questions in
one call and splits the call's usage over them. When the round ends early (an
earlier slot clarifies, declines or is refused), the answers it fetched ahead
are not decisions of the resolution and stay off the ledger — but the provider
billed them. Pinned here: the row's tokens equal what the provider billed,
whether the batch was used whole, cut short, or cut by a refusal; and a split
call's shares add up to the call.

A provider that fails partway was billed too: for the votes that answered
before one errored, for a reply that came back unreadable, for the rounds of a
resolution that succeeded before a later one failed. That spend rides the
failure, and lands on the answer's row (a typed-only refusal or an untyped
fallback) or, for a routing outage, on the routing row observe sums.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from services.contracts.errors import ProviderError
from services.contracts.fakes import EchoQueryGenerator, FakeConnector, HashEmbedder
from services.contracts.protocols import (
    CacheableBlock,
    Decision,
    DecisionUsage,
    LLMResult,
    Message,
    Option,
    spent_by,
)
from services.llm.typesafe import TypesafeDecider
from services.llm.vote_decider import DeciderStarved, VoteDecider
from services.router import DECIDER_DOWN, CoverageProfile, Router
from services.router.decider import LlmDecider, route_with_llm
from services.runtime import typed_health
from services.runtime.pipeline import run_query
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
from tests.test_typed_resolver import DOMAINS, TODAY, _model

OPTS = [Option("revenue", "money collected"), Option("order_count")]


class _BillingBatcher:
    """Batches every round into one call and bills it: 10 input tokens a
    question plus one, one output token a question plus one — never a
    multiple of the round's size, so a fractional share would show."""

    provider = "batch"

    def __init__(self, answers: dict[str, str | None], refuse: tuple[str, ...] = ()) -> None:
        self._answers = answers
        self._refuse = set(refuse)
        self.calls: list[list[str]] = []
        self.billed_in = 0
        self.billed_out = 0

    def _bill(self, n: int) -> list[DecisionUsage]:
        tokens_in, tokens_out = 10 * n + 1, n + 1
        self.billed_in += tokens_in
        self.billed_out += tokens_out
        return DecisionUsage(calls=1, input_tokens=tokens_in, output_tokens=tokens_out).split(n)

    def _one(self, key: str, usage: DecisionUsage) -> Decision:
        if key in self._refuse:
            return Decision(
                chosen=None, probs=None, provider=self.provider, refused=True, usage=usage
            )
        chosen = self._answers.get(key)
        return Decision(
            chosen=chosen, probs={chosen or "none": 1.0}, provider=self.provider, usage=usage
        )

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.calls.append(["<single>"])
        return self._one("<single>", self._bill(1)[0])

    def decide_many(self, question: str, questions: dict[str, Any]) -> dict[str, Decision]:
        self.calls.append(list(questions))
        shares = self._bill(len(questions))
        return {k: self._one(k, s) for k, s in zip(questions, shares, strict=True)}


ALL_USED = (
    "revenue by country for shipped orders last month",
    {"shape": "aggregate", "metric:0": "revenue", "filter:0": "status", "filter:1": None},
    (),
)
# The second metric is named but not picked: the metric slot clarifies while
# the first filter, asked in the same round, is still waiting.
EARLY_END = (
    "revenue and order count by month",
    {"shape": "aggregate", "metric:0": "revenue", "grain": "month"},
    (),
)
# The grain is refused mid-round: the second dimension before it was used,
# the first filter after it never is.
REFUSED = (
    "revenue by country by month",
    {"shape": "aggregate", "metric:0": "revenue", "dimension:0": "country"},
    ("grain",),
)


def _spent(records: list[Any]) -> tuple[float, float]:
    return (
        sum(r.usage["input_tokens"] for r in records if r.usage),
        sum(r.usage["output_tokens"] for r in records if r.usage),
    )


@pytest.mark.parametrize(
    ("case", "unused"),
    [(ALL_USED, False), (EARLY_END, True), (REFUSED, True)],
    ids=["all-used", "early-end", "refused-mid-batch"],
)
def test_the_resolution_accounts_for_every_billed_token(case: Any, unused: bool) -> None:
    question, answers, refuse = case
    decider = _BillingBatcher(answers, refuse)
    res = TypedResolver(decider, domains=DOMAINS, today=TODAY).resolve(question, _model())  # type: ignore[arg-type]
    assert any(len(c) > 1 for c in decider.calls)  # a round was batched
    assert bool(res.unused) is unused
    # The unused answers stay off the ledger: no slot reads them as taken.
    assert not {id(r) for r in res.unused} & {id(r) for r in res.decisions}
    assert _spent([*res.decisions, *res.unused]) == (decider.billed_in, decider.billed_out)
    if refuse:
        assert res.decline and "refused" in res.decline
        assert [r.slot for r in res.unused] == ["filter"]


@pytest.mark.parametrize(
    ("case", "status"),
    [(ALL_USED, "ok"), (EARLY_END, "clarification"), (REFUSED, "refused")],
    ids=["all-used", "early-end", "refused-mid-batch"],
)
def test_the_answer_row_carries_the_whole_call(case: Any, status: str) -> None:
    question, answers, refuse = case
    decider = _BillingBatcher(answers, refuse)
    res = run_query(
        question=question,
        lens_name="sales",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(),
        generator=TypedIntentGenerator(TypedResolver(decider, domains=DOMAINS, today=TODAY)),  # type: ignore[arg-type]
        composer=None,
    )
    assert res.trace.status == status, res.trace.error
    assert res.trace.ai_input_tokens == decider.billed_in
    assert res.trace.ai_output_tokens == decider.billed_out


@pytest.mark.parametrize(("tokens", "n"), [(91, 3), (100, 3), (101, 2), (7, 4), (0, 3)])
def test_a_split_call_adds_up_to_the_call(tokens: int, n: int) -> None:
    shares = DecisionUsage(calls=1, input_tokens=tokens, output_tokens=n + 1).split(n)
    assert len(shares) == n
    assert sum(s.input_tokens for s in shares) == tokens
    assert sum(s.output_tokens for s in shares) == n + 1
    assert all(s.input_tokens == int(s.input_tokens) for s in shares)  # whole tokens
    assert sum(s.calls for s in shares) == pytest.approx(1.0)
    # Rounded per decision, as an answer row charges them, nothing is lost.
    assert sum(round(s.as_dict()["input_tokens"]) for s in shares) == tokens


# ── a provider that fails partway ────────────────────────────────────────────


class _OutageAt(_BillingBatcher):
    """Bills like ``_BillingBatcher`` until call number *at*, which fails
    before anything is billed (the provider went down)."""

    def __init__(self, answers: dict[str, str | None], at: int) -> None:
        super().__init__(answers)
        self._at = at

    def _bill(self, n: int) -> list[DecisionUsage]:
        if len(self.calls) >= self._at:
            raise ProviderError("batch", "upstream unavailable", status=502)
        return super()._bill(n)


class _MeteredLLM:
    """A chat model for the vote decider: replies in order, bills 100 input
    and 7 output tokens a reply, and from reply *fail_from* (0-based) on, the
    provider is down. Thread-safe: votes run on a pool."""

    def __init__(self, replies: list[str], fail_from: int) -> None:
        self._replies = replies
        self._fail_from = fail_from
        self._lock = threading.Lock()
        self.calls = 0
        self.billed_in = 0
        self.billed_out = 0

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        with self._lock:
            n = self.calls
            self.calls += 1
            if n >= self._fail_from:
                raise ProviderError("fast", "upstream unavailable", status=503)
            self.billed_in += 100
            self.billed_out += 7
        return LLMResult(
            text=self._replies[min(n, len(self._replies) - 1)], input_tokens=100, output_tokens=7
        )


def _total(spent: list[tuple[str, DecisionUsage]]) -> tuple[float, float]:
    return sum(u.input_tokens for _p, u in spent), sum(u.output_tokens for _p, u in spent)


def test_a_vote_that_fails_carries_the_votes_that_answered() -> None:
    llm = _MeteredLLM(["revenue"], fail_from=3)
    decider = VoteDecider(llm, "fast-model", k=5, concurrency=1)
    with pytest.raises(ProviderError) as failed:
        decider.decide(question="q", context="c", options=OPTS, allow_none=True)
    assert llm.billed_in == 300  # three votes answered before the outage
    assert _total(spent_by(failed.value)) == (llm.billed_in, llm.billed_out)
    assert {p for p, _u in spent_by(failed.value)} == {decider.provider}


@pytest.mark.parametrize("k", [1, 5])
def test_a_starved_vote_carries_what_its_replies_cost(k: int) -> None:
    llm = _MeteredLLM(["(no answer)"], fail_from=99)
    with pytest.raises(DeciderStarved) as starved:
        VoteDecider(llm, "fast-model", k=k).decide(
            question="q", context="c", options=OPTS, allow_none=False
        )
    assert llm.billed_in == 100 * k
    assert _total(spent_by(starved.value)) == (llm.billed_in, llm.billed_out)


def test_an_unreadable_typed_reply_carries_the_call_it_billed() -> None:
    body = {
        "answers": {"a": {"choice": "revenue", "probabilities": {"revenue": 1.0}}},
        "usage": {"input_tokens": 60, "output_tokens": 2},
    }
    d = TypesafeDecider(
        "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)), attempts=1
    )
    spec = ("c", OPTS, True)
    with pytest.raises(ProviderError) as failed:
        d.decide_many("q", {"a": spec, "b": spec})  # answer b is missing
    assert _total(spent_by(failed.value)) == (60, 2)


# Three rounds batch this question: the outage hits the second or the third.
@pytest.mark.parametrize("at", [2, 3])
def test_a_resolution_that_fails_carries_the_rounds_before_it(at: int) -> None:
    question, answers, _ = ALL_USED
    decider = _OutageAt(answers, at)
    generator = TypedIntentGenerator(TypedResolver(decider, domains=DOMAINS, today=TODAY))  # type: ignore[arg-type]
    with pytest.raises(typed_health.TypedUnavailable) as down:
        generator.generate(
            question=question, semantic_model=_model(), prose_context=[], dialect="duckdb"
        )
    assert decider.billed_in > 0
    assert _total(down.value.spent) == (decider.billed_in, decider.billed_out)
    typed_health.reset()


def _ask_typed(decider: Any, question: str, *, fallback: bool) -> Any:
    return run_query(
        question=question,
        lens_name="sales",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(),
        generator=TypedIntentGenerator(TypedResolver(decider, domains=DOMAINS, today=TODAY)),
        escalate_generator=EchoQueryGenerator("SELECT 1 AS n") if fallback else None,
        composer=None,
    )


@pytest.mark.parametrize("fallback", [False, True], ids=["typed-only", "untyped-fallback"])
@pytest.mark.parametrize("at", [2, 3])
def test_the_answer_row_carries_what_a_failed_resolution_billed(at: int, fallback: bool) -> None:
    question, answers, _ = ALL_USED
    decider = _OutageAt(answers, at)
    res = _ask_typed(decider, question, fallback=fallback)
    typed_health.reset()
    assert res.trace.status == ("ok" if fallback else "refused"), res.trace.error
    assert (res.trace.ai_input_tokens, res.trace.ai_output_tokens) == (
        decider.billed_in,
        decider.billed_out,
    )


def test_the_answer_row_carries_the_votes_of_a_decision_cut_by_an_outage() -> None:
    """Vote decider, k=3, one call a vote: the shape is decided (3 votes), the
    metric's first vote answers and its second fails."""
    llm = _MeteredLLM(["aggregate"] * 3 + ["revenue"], fail_from=4)
    decider = VoteDecider(llm, "fast-model", k=3, concurrency=1)
    res = _ask_typed(decider, ALL_USED[0], fallback=False)
    typed_health.reset()
    assert res.trace.status == "refused", res.trace.error
    assert llm.billed_in == 400
    assert (res.trace.ai_input_tokens, res.trace.ai_output_tokens) == (
        llm.billed_in,
        llm.billed_out,
    )


ROUTED = [
    CoverageProfile(lens="billing", anchors=["net invoiced amount"], description="invoices"),
    CoverageProfile(lens="pipeline", anchors=["deal win rate"], description="deals"),
]


def test_a_routing_outage_carries_the_votes_that_answered() -> None:
    llm = _MeteredLLM(["billing"], fail_from=2)
    decider = LlmDecider(None, "fast-model", decider=VoteDecider(llm, "fast-model", concurrency=1))
    d = route_with_llm(
        Router(ROUTED, HashEmbedder()),
        {p.lens: p for p in ROUTED},
        decider,
        "what did we invoice",
        all_lenses=True,
    )
    assert not d.covered and d.degraded == DECIDER_DOWN and d.decision is None
    assert llm.billed_in == 200
    assert _total(d.spent) == (llm.billed_in, llm.billed_out)
