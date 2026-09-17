"""The probability regime: measured decisions become act | clarify | decline.

Deterministic, clarify the only fallthrough, and a decision that measured
nothing reproduces today's act-or-decline byte-for-byte.
"""

from __future__ import annotations

import pytest

from services.contracts.protocols import Decision
from services.runtime import decision_policy as dp

P = {"t": dp.Policy(act_p=0.8, act_margin=0.3, decline_p=0.8)}


@pytest.mark.parametrize(
    ("chosen", "probs", "want"),
    [
        ("a", {"a": 0.9, "b": 0.1}, "act"),  # sure and clear
        ("a", {"a": 0.8, "b": 0.6}, "clarify"),  # sure but not clear: margin 0.2 < 0.3
        ("a", {"a": 0.6, "b": 0.2}, "clarify"),  # clear but not sure
        (None, {"a": 0.1, "none": 0.9}, "decline"),  # confidently nothing fits
        (None, {"a": 0.4, "none": 0.6}, "clarify"),  # maybe nothing fits — ask
        ("a", None, "act"),  # unmeasured: legacy act
        (None, None, "decline"),  # unmeasured: legacy decline
    ],
)
def test_verdict_table(chosen: str | None, probs: dict[str, float] | None, want: str) -> None:
    d = Decision(chosen=chosen, probs=probs, provider="x")
    assert dp.verdict(d, "t", P) == want
    assert dp.verdict(d, "t", P) == want  # same input, same verdict


def test_defaults_cover_every_slot_and_say_which_are_measured() -> None:
    for slot in ("lens", "certified_equivalent", "metric", "grain", "filter_value"):
        assert slot in dp.DEFAULTS and slot in dp.STAMPED_BY
    # The lens slot was measured; naming the run is the only way to stamp one.
    assert dp.STAMPED_BY["lens"] and "calibration run" in dp.STAMPED_BY["lens"]
    # Until a run stamps them, nobody may read the others as measured.
    assert all(dp.STAMPED_BY[s] is None for s in ("certified_equivalent", "metric"))


def test_describe_names_the_choice_and_the_measurement() -> None:
    d = Decision(chosen="a", probs={"a": 0.75, "b": 0.25}, provider="vote:k=4")
    line = dp.describe(d, options_shown=2)
    assert line.startswith("a p=0.75 margin=0.50")
    assert "vote:k=4" in line
    assert dp.describe(Decision(chosen=None, probs=None, provider="x"), 3) == "none (unmeasured, x)"


def test_a_typed_decision_provider_acts_unless_none_wins() -> None:
    """The typed regime: no act bar — the top choice acts at any p; `none`
    winning declines. The vote keeps its measured bars (its p steps in fifths)."""
    low = Decision(chosen="a", probs={"a": 0.42, "b": 0.3, "c": 0.28}, provider="typesafe:jev")
    assert dp.verdict(low, "lens") == "act"
    assert dp.verdict(low, "filter") == "act"
    none = Decision(chosen=None, probs={"none": 0.4, "a": 0.35}, provider="typesafe:jev")
    assert dp.verdict(none, "dimension") == "decline"
    vote = Decision(chosen="a", probs={"a": 0.4, "b": 0.6 - 0.2}, provider="vote:m:k=5")
    assert dp.verdict(vote, "lens") == "clarify"
    assert dp.policy_for("metric", "typesafe:jev") == dp.ARGMAX
    assert dp.STAMPED_BY["typesafe/*"] and dp.STAMPED_BY["typesafe/lens"]


def test_a_measured_bar_discloses_an_acted_decision_below_it() -> None:
    from services.contracts.resolution import DecisionRecord

    def rec(slot: str, p: float, provider: str = "typesafe:jev") -> DecisionRecord:
        return DecisionRecord(slot=slot, chosen="x", p=p, verdict="act", provider=provider)

    assert dp.disclose_below("lens", "typesafe:jev") == 0.9
    assert dp.disclose_below("metric", "typesafe:jev") is None  # unmeasured is not a bar
    assert dp.disclose_below("lens", "vote:m:k=5") is None
    low = dp.low_confidence([rec("lens", 0.7), rec("lens", 0.95), rec("metric", 0.2)])
    assert [(r.slot, r.p, bar) for r, bar in low] == [("lens", 0.7, 0.9)]
    assert "lens decided as x at p=0.70, below the measured 0.90 bar" == dp.describe_low(low)
    # A clarified or declined decision is not "acted below the bar".
    asked = DecisionRecord(
        slot="lens", chosen="x", p=0.7, verdict="clarify", provider="typesafe:jev"
    )
    assert dp.low_confidence([asked]) == []


def test_decisions_are_priced_by_the_model_inside_the_provider_id() -> None:
    from services.observability import cost

    assert cost.decision_model("vote:deepseek-v4-flash:k=5") == "deepseek-v4-flash"
    assert cost.decision_model("typesafe:jev-latest") == "jev-latest"
    priced = cost.decisions_cost_usd({"vote:deepseek-v4-flash:k=5": (1_000_000, 0)})
    assert priced == 0.14
    # jev-latest is priced (input only): 100 tokens at $0.042/M, never rounded to $0
    assert cost.decisions_cost_usd({"typesafe:jev-latest": (100, 0)}) == round(100 / 1e6 * 0.042, 6)
    assert (
        cost.decisions_cost_usd({"typesafe:unknown-model": (100, 0)}) is None
    )  # unpriced, never 0
    assert cost.decisions_cost_usd({"typesafe:jev-latest": (0, 0)}) == 0.0
