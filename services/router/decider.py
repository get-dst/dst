"""LLM coverage decider — the routing signal that actually understands coverage.

Cosine over a lens's anchor strings is one weak signal: it OVER-DECLINES paraphrases
(they fall below the floor) and MIS-ROUTES noise (an uncovered question scores ~0.9
against a lens that cannot answer it). As catalogues grow the floor/margin gate
over-declines most paraphrases, where an LLM decider reads coverage correctly.

So the division of labor is deterministic: cosine is the
RECALL device — a top-k shortlist that bounds the prompt, plus one near-verbatim
fast-path (>= confident) — and the LLM decider is the decision-maker for everything
else. The floor/margin arm is deleted as a decision path: it routed almost nothing at
scale and mis-routed when it did (near-tie scores flake either way).

And the decider fails LOUD: on provider error or a starved/unparseable
reply, the router declines with a ``DECIDER_DOWN`` degraded marker — it never
falls back to the mid-band cosine route. A refusal is an
outcome; a silent mis-route is the defect.
"""

from __future__ import annotations

import logging

from services.contracts.protocols import Decider, Decision, LLMProvider, Option
from services.llm.vote_decider import DEFAULT_K, DeciderStarved, VoteDecider
from services.router import DECIDER_DOWN, CoverageProfile, RouteDecision, Router
from services.runtime import decision_policy

log = logging.getLogger("dst")

# Shortlist floor: never show the decider fewer than this many candidates.
PREFILTER_K = 5
# Shortlist ceiling: the catalogue block grows linearly with k — cap it so the
# prompt stays inside the fast tier's budget at any catalogue size.
_MAX_SHORTLIST = 12


def shortlist_k(catalogue_size: int) -> int:
    """Half the catalogue, never fewer than PREFILTER_K, capped by the prompt
    budget. Frozen at 5 it silently truncates recall as a catalogue grows — the
    gold lens falls off the shortlist the decider never sees. Scaling it keeps
    the gold lens in view (pinned by the recall gate, tests/test_router_gate.py)."""
    return min(max(PREFILTER_K, -(-catalogue_size // 2)), _MAX_SHORTLIST)


__all__ = ["DeciderStarved", "LlmDecider", "route_with_llm", "shortlist_k"]

_CONTEXT = (
    "Route a data question to the ONE governed lens that covers it, or none. A lens covers "
    "a question when the answer lies within its governed metrics OR its stated "
    "purpose/domain — a question that names a lens's subject area is covered even if it "
    "does not name a specific metric (e.g. 'numbers for the board' → the board-reporting "
    "lens). Reply none only when NO lens's metrics or purpose fit; never stretch an "
    "unrelated lens."
)


def _options(profiles: list[CoverageProfile]) -> list[Option]:
    return [
        Option(
            p.lens,
            f"governed metrics: {'; '.join(p.anchors)}."
            + (f" ({p.description})" if p.description else ""),
        )
        for p in profiles
    ]


def _parse(text: str, names: set[str]) -> str | None:
    """A lens name appearing in any form (customer_value / "customer value" / **bolded** /
    "the sales lens"). Longest name first so customer_value beats a stray "customer"."""
    low = text.strip().lower()
    for name in sorted(names, key=len, reverse=True):
        if name in low or name.replace("_", " ") in low:
            return name
    return None


class LlmDecider:
    """Picks the covering lens among shortlisted candidates, or declines.

    Sits on the typed-decision seam: the lens choice is a closed-set decision
    over the shortlist, made by a ``Decider`` (today ``VoteDecider`` over the
    fast tier; a typed-decision model plugs in at the same seam). ``k`` is the
    vote count — 1 measures nothing and is the legacy single call.

    Raises ``DeciderStarved`` on an empty/unparseable reply — only an explicit
    'none' is a decline; everything else is an outage the caller must surface.
    """

    def __init__(
        self,
        provider: LLMProvider | None,
        model: str,
        *,
        k: int = DEFAULT_K,
        decider: Decider | None = None,
    ) -> None:
        if decider is None:
            assert provider is not None, "LlmDecider needs a provider or a decider"
            decider = VoteDecider(provider, model, k=k)
        self._decider: Decider = decider
        # Routes over every lens, no cosine, when the wrapped decider is a
        # typed-decision provider (see TypesafeDecider.typed_decisions).
        self.typed_decisions = bool(getattr(decider, "typed_decisions", False))

    def decide_measured(self, question: str, candidates: list[CoverageProfile]) -> Decision:
        decider: Decider | None = getattr(self, "_decider", None)
        if decider is None:
            # A subclass that overrides decide() without a provider (tests' gold
            # deciders): its answer is a choice with nothing measured.
            return Decision(chosen=self.decide(question, candidates), probs=None, provider="stub")
        if not candidates:
            return Decision(chosen=None, probs=None, provider="none-offered")
        return decider.decide(
            question=question, context=_CONTEXT, options=_options(candidates), allow_none=True
        )

    def decide(self, question: str, candidates: list[CoverageProfile]) -> str | None:
        return self.decide_measured(question, candidates).chosen


def route_with_llm(
    router: Router,
    profiles_by_lens: dict[str, CoverageProfile],
    decider: LlmDecider,
    question: str,
    *,
    k: int | None = None,
    all_lenses: bool | None = None,
) -> RouteDecision:
    """Cosine shortlist → LLM coverage decision, with ONE deterministic fast-path.

    ``all_lenses``: no cosine at all — every published lens is an option and
    the decider decides, the verbatim fast path included. The default for a
    typed-decision provider (``decider.typed_decisions``), measured equal to
    the shortlist on the held-out routing set (2026-09-17)
    and simpler; the vote decider keeps the shortlist its prompt budget needs.

    A near-exact match (>= confident) routes without the LLM. Everything else goes
    to the decider over the top-k shortlist — the floor/margin arm is gone: in the
    compressed mid-band the runner-up sits a hundredth of a cosine point away, so
    the margin rule was resolving real routing decisions by flake. Ties and the
    whole mid-band are the decider's job now. Decider failure — provider
    error or a starved reply — DECLINES with the ``DECIDER_DOWN`` marker rather
    than falling back to the deleted arm: loud degradation over silent mis-routes.
    """
    scored = router.scored(question)
    if not scored:
        return RouteDecision(False, None, 0.0, [], "no lenses to route to")
    best_lens, best_score = scored[0]
    if all_lenses is None:
        all_lenses = bool(getattr(decider, "typed_decisions", False))
    if all_lenses:
        k = len(scored)
    elif best_score >= router.confident:
        return RouteDecision(
            True,
            best_lens,
            best_score,
            scored[1:],
            "routed (cosine confident)",
            decided_by="cosine-verbatim",
        )

    if k is None:
        k = shortlist_k(len(scored))
    shortlist = [profiles_by_lens[lens] for lens, _ in scored[:k] if lens in profiles_by_lens]
    try:
        decision = (
            decider.decide_measured(question, shortlist)
            if hasattr(decider, "decide_measured")
            else Decision(chosen=decider.decide(question, shortlist), probs=None, provider="stub")
        )
    except Exception:
        log.exception("coverage decider unavailable — declining (degraded), never mid-band cosine")
        return RouteDecision(
            False,
            None,
            best_score,
            scored,
            "routing degraded: the coverage decider is unavailable — declining rather than "
            "guessing from the ambiguous cosine band",
            degraded=DECIDER_DOWN,
        )
    verdict = decision_policy.verdict(decision, "lens")
    if verdict == "clarify":
        # Measured, and not sure enough to act: an AMBIGUITY, which is a
        # coverage signal (name the lens), never an outage — no degraded mark.
        ranked = sorted((decision.probs or {}).items(), key=lambda kv: -kv[1])
        lenses = [k for k, _v in ranked[:2] if k != "none"]
        # Two lenses close together is an ambiguity between them; one lens
        # against `none` is doubt that any lens covers it — say which.
        reason = (
            f"ambiguous between {lenses[0]} and {lenses[1]} — name the lens"
            if len(lenses) == 2
            else f"unsure whether {lenses[0] if lenses else 'any lens'} covers this — name the lens"
        )
        return RouteDecision(
            False,
            None,
            best_score,
            scored,
            reason,
            decided_by="decider",
            decision=decision,
            verdict=verdict,
        )
    chosen = decision.chosen
    if chosen is None:
        return RouteDecision(
            False,
            None,
            best_score,
            scored,
            "no governed lens covers this (llm)",
            decided_by="decider",
            decision=decision,
            verdict=verdict,
        )
    score = dict(scored).get(chosen, best_score)
    alternatives = [(lens, s) for lens, s in scored if lens != chosen]
    return RouteDecision(
        True,
        chosen,
        score,
        alternatives,
        "routed (llm)",
        decided_by="decider",
        decision=decision,
        verdict=verdict,
    )
