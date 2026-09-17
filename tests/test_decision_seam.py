"""The typed-decision seam: a closed-set choice with the probability that was
MEASURED for it — never a fabricated one.

`Decision.probs is None` is load-bearing: a decider that measured nothing says
so, the policy falls back to act-or-decline, and the calibration report prints
UNTESTED instead of a number. The fake replays decisions in order so the
serving paths can be driven deterministically.
"""

from __future__ import annotations

from services.contracts.fakes import ScriptedDecider
from services.contracts.protocols import Decider, Decision, Option


def test_scripted_decider_is_a_decider_and_replays_in_order() -> None:
    d1 = Decision(chosen="a", probs={"a": 0.8, "b": 0.2}, provider="scripted")
    d2 = Decision(chosen=None, probs={"a": 0.1, "none": 0.9}, provider="scripted")
    fake = ScriptedDecider([d1, d2])
    assert isinstance(fake, Decider)
    opts = [Option("a", "first"), Option("b")]
    assert fake.decide(question="q", context="", options=opts, allow_none=True) is d1
    assert fake.decide(question="q", context="", options=opts, allow_none=True) is d2
    assert (
        fake.decide(question="q", context="", options=opts, allow_none=True) is d2
    )  # last repeats
    assert fake.calls[0] == {"question": "q", "options": ["a", "b"]}


def test_probability_arithmetic() -> None:
    d = Decision(chosen="a", probs={"a": 0.6, "b": 0.3, "none": 0.1}, provider="x")
    assert (d.p, d.runner_up, d.margin) == (0.6, 0.3, 0.3)
    none = Decision(chosen=None, probs={"a": 0.2, "none": 0.8}, provider="x")
    assert (none.p, none.runner_up) == (0.8, 0.2)
    alone = Decision(chosen="a", probs={"a": 1.0}, provider="x")
    assert (alone.runner_up, alone.margin) == (0.0, 1.0)


def test_unmeasured_probability_is_none_never_one() -> None:
    d = Decision(chosen="a", probs=None, provider="vote:k=1")
    assert d.p is None and d.runner_up is None and d.margin is None
