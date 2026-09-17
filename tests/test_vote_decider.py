"""VoteDecider: the one probability a chat model can measure honestly — how
often it picks the same option — and k = 1 is today's behaviour byte-for-byte.
"""

from __future__ import annotations

import pytest

from services.contracts.fakes import ScriptedLLM
from services.contracts.protocols import Decider, Option
from services.llm.vote_decider import DeciderStarved, VoteDecider, parse_choice

OPTS = [Option("customer_value", "clv"), Option("sales", "bookings"), Option("customer")]


def test_vote_probabilities_are_vote_shares() -> None:
    llm = ScriptedLLM(["customer_value", "customer_value", "sales", "customer_value", "none"])
    d = VoteDecider(llm, "m", k=5, concurrency=1).decide(
        question="q", context="", options=OPTS, allow_none=True
    )
    assert isinstance(VoteDecider(llm, "m"), Decider)
    assert d.chosen == "customer_value"
    assert d.probs == {"customer_value": 0.6, "sales": 0.2, "customer": 0.0, "none": 0.2}
    assert d.p == 0.6 and d.margin == pytest.approx(0.4)
    assert d.provider == "vote:m:k=5"


def test_k_of_one_measures_nothing() -> None:
    d = VoteDecider(ScriptedLLM(["sales"]), "m", k=1).decide(
        question="q", context="", options=OPTS, allow_none=True
    )
    assert d.chosen == "sales" and d.probs is None
    none = VoteDecider(ScriptedLLM(["none"]), "m", k=1).decide(
        question="q", context="", options=OPTS, allow_none=True
    )
    assert none.chosen is None and none.probs is None


def test_garbage_replies_are_starvation_not_a_decline() -> None:
    with pytest.raises(DeciderStarved):
        VoteDecider(ScriptedLLM([""]), "m", k=1).decide(
            question="q", context="", options=OPTS, allow_none=True
        )
    with pytest.raises(DeciderStarved):
        VoteDecider(ScriptedLLM(["", "???", ""]), "m", k=3, concurrency=1).decide(
            question="q", context="", options=OPTS, allow_none=True
        )
    # One garbage vote among real ones is dropped from the denominator.
    d = VoteDecider(ScriptedLLM(["sales", "", "sales"]), "m", k=3, concurrency=1).decide(
        question="q", context="", options=OPTS, allow_none=False
    )
    assert d.probs == {"customer_value": 0.0, "sales": 1.0, "customer": 0.0}


def test_none_is_garbage_when_none_was_not_offered() -> None:
    with pytest.raises(DeciderStarved):
        VoteDecider(ScriptedLLM(["none"]), "m", k=1).decide(
            question="q", context="", options=OPTS, allow_none=False
        )


def test_parse_prefers_the_longest_name() -> None:
    names = {o.name for o in OPTS}
    assert parse_choice("**customer value**", names) == "customer_value"
    assert parse_choice("the customer lens", names) == "customer"
    assert parse_choice("none of these", names) is None
    assert parse_choice("", names) is False


def test_ties_break_by_option_order_deterministically() -> None:
    llm = ScriptedLLM(["sales", "customer_value"])
    a = VoteDecider(llm, "m", k=2, concurrency=1).decide(
        question="q", context="", options=OPTS, allow_none=False
    )
    b = VoteDecider(ScriptedLLM(["customer_value", "sales"]), "m", k=2, concurrency=1).decide(
        question="q", context="", options=OPTS, allow_none=False
    )
    assert a.chosen == b.chosen == "customer_value"


def test_the_vote_reports_k_calls_and_their_tokens() -> None:
    from services.contracts.fakes import ScriptedLLM
    from services.contracts.protocols import Option
    from services.llm.vote_decider import VoteDecider

    llm = ScriptedLLM(["a", "a", "b", "a", "a"])
    d = VoteDecider(llm, "m", k=5).decide(
        question="q", context="c", options=[Option("a"), Option("b")], allow_none=False
    )
    assert d.usage is not None and d.usage.calls == 5.0
    assert d.usage.input_tokens == 5.0 and d.usage.output_tokens == 5.0  # the fake: 1 + 1 each
    assert d.usage.latency_s >= 0.0
