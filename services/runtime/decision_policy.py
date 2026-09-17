"""The probability regime: a measured decision becomes act, clarify or decline.

One policy per decision slot — how sure, and by how much, a decider must be
before its choice is acted on without asking. dst-owned constants, like the
router's floor: never a user knob. Deterministic (rule 5): the same Decision
under the same policy yields the same verdict, and clarify is the only
fallthrough — a decision that is not sure enough to act asks, it never guesses.

A Decision that measured nothing (``probs is None``) reproduces the legacy
behaviour byte-for-byte: act on a choice, decline on none. The regime only
binds where a probability was actually measured.

A slot's defaults are PLACEHOLDERS until the calibration experiment stamps
them (scripts/decision_calibration.py) — STAMPED_BY names the run per slot, or
None. Callers may pass a policy so the eval grid can vary it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.contracts.protocols import Decision
from services.contracts.resolution import DecisionRecord

Verdict = Literal["act", "clarify", "decline"]


@dataclass(frozen=True)
class Policy:
    # Act on the chosen option when p(chosen) >= act_p AND p(chosen) - p(runner-up) >= act_margin.
    act_p: float
    act_margin: float
    # Decline (rather than clarify) when the decider chose `none` with p(none) >= decline_p.
    # Only meaningful for decisions that allow none.
    decline_p: float


DEFAULTS: dict[str, Policy] = {
    # Measured: at k=5 the coverage curve reads 100% coverage / 4% error among
    # acted at p>=0.6 (today's single call errs 5.8%), so the lowest threshold
    # that acts no worse than today is 0.6; with five votes p=0.6 already means
    # a 0.2 margin over the runner-up. none at >=0.6 declines, below it asks.
    "lens": Policy(act_p=0.6, act_margin=0.2, decline_p=0.6),
    # PLACEHOLDERS — labelled so until a run stamps them (STAMPED_BY below).
    "certified_equivalent": Policy(act_p=0.9, act_margin=0.5, decline_p=1.0),
    "metric": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "grain": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "filter_value": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "shape": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "entity": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "dimension": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "definition": Policy(act_p=0.8, act_margin=0.3, decline_p=1.0),
    "filter": Policy(act_p=0.8, act_margin=0.3, decline_p=0.8),
    "filter_op": Policy(act_p=0.8, act_margin=0.3, decline_p=1.0),
    "judge_scope": Policy(act_p=0.8, act_margin=0.3, decline_p=1.0),
    # Which declared reading of an ambiguous term the question means; a
    # confident `none` never declines — the clarify rail is the outcome.
    "reading": Policy(act_p=0.8, act_margin=0.3, decline_p=1.0),
}
# The calibration run that set each slot's policy; None = placeholder. A default
# changed without a fresh entry here is visible in review — that is the point.
STAMPED_BY: dict[str, str | None] = {
    "lens": "routing calibration run, 2026-09-16 (deepseek-v4-flash, k=5)",
    "typesafe/*": "act unless none wins (stamped 2026-09-17); bars are disclosure",
    "typesafe/lens": "routing calibration run, 2026-09-17 (jev-latest) — disclosure bar",
    "typesafe/routing": (
        "all lenses, no cosine — routing calibration run, 2026-09-17 "
        "(measured equal to the cosine shortlist)"
    ),
    "certified_equivalent": None,
    "metric": None,
    "grain": None,
    "filter_value": None,
    "shape": None,
    "entity": None,
    "dimension": None,
    "definition": None,
    "filter": None,
    "filter_op": None,
    "judge_scope": None,
    "reading": None,
}


# The typed-decision regime (stamped 2026-09-17): a typed-decision provider ACTS
# on its top choice unless `none` won — no per-slot act bar anyone typed in.
# What a measured bar becomes is DISCLOSURE: an acted decision below the bar
# its slot was measured at is said out loud on the answer's verification (the
# grade caps at partial), never refused. A bar exists only where a calibration
# run stamped one (DISCLOSE_BELOW); the slot lane's coverage curve is where new
# ones come from. The vote decider keeps its act bars: five votes step in
# fifths, and its probabilities were measured under them.
ARGMAX = Policy(act_p=0.0, act_margin=0.0, decline_p=0.0)

# Per-provider overrides, keyed by the provider id's prefix (Decision.provider
# before the first ':').
PROVIDER_DEFAULTS: dict[str, dict[str, Policy]] = {
    "typesafe": dict.fromkeys(DEFAULTS, ARGMAX),
}

# Measured bars, per provider prefix and slot: an ACTED decision below the bar
# is disclosed. typesafe/lens — the routing calibration run of 2026-09-17: at
# p>=0.90 the acted decisions were right 98% of the time with 94% coverage.
DISCLOSE_BELOW: dict[str, dict[str, float]] = {
    "typesafe": {"lens": 0.9},
}


def disclose_below(slot: str, provider: str) -> float | None:
    """The measured bar an acted decision on this slot is disclosed under, or
    None where no run has measured one (an unmeasured bar is not a bar)."""
    return DISCLOSE_BELOW.get(provider.split(":", 1)[0], {}).get(slot)


def low_confidence(records: list[DecisionRecord]) -> list[tuple[DecisionRecord, float]]:
    """The acted decisions that fell below their slot's measured bar."""
    out: list[tuple[DecisionRecord, float]] = []
    for r in records:
        bar = disclose_below(r.slot, r.provider)
        if r.verdict == "act" and r.p is not None and bar is not None and r.p < bar:
            out.append((r, bar))
    return out


def describe_low(low: list[tuple[DecisionRecord, float]]) -> str:
    """One line naming each low-confidence decision: slot, choice, p, bar."""
    return "; ".join(
        f"{r.slot} decided as {r.chosen or 'none'} at p={r.p:.2f}, below the measured {bar:.2f} bar"
        for r, bar in low
        if r.p is not None
    )


def policy_for(slot: str, provider: str, policies: dict[str, Policy] | None = None) -> Policy:
    if policies is not None:
        return policies[slot]
    prefix = provider.split(":", 1)[0]
    return PROVIDER_DEFAULTS.get(prefix, {}).get(slot, DEFAULTS[slot])


def verdict(d: Decision, slot: str, policies: dict[str, Policy] | None = None) -> Verdict:
    """act | clarify | decline for one measured decision under the slot's policy
    (the provider's override when one was measured for it)."""
    policy = policy_for(slot, d.provider, policies)
    if d.probs is None:
        # Nothing measured: today's behaviour — a choice acts, none declines.
        return "act" if d.chosen is not None else "decline"
    p, margin = d.p, d.margin
    assert p is not None and margin is not None  # probs set ⇒ both derived
    if d.chosen is None:
        return "decline" if p >= policy.decline_p else "clarify"
    if p >= policy.act_p and margin >= policy.act_margin:
        return "act"
    return "clarify"


def describe(d: Decision, options_shown: int) -> str:
    """One audit line: what was chosen, at what probability, against what."""
    if d.probs is None:
        return f"{d.chosen or 'none'} (unmeasured, {d.provider})"
    ranked = sorted(d.probs.items(), key=lambda kv: -kv[1])
    top = ", ".join(f"{k} {v:.2f}" for k, v in ranked[:3])
    return (
        f"{d.chosen or 'none'} p={d.p:.2f} margin={d.margin:.2f} [{top}] "
        f"over {options_shown} ({d.provider})"
    )
