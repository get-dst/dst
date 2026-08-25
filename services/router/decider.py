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

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.router import DECIDER_DOWN, CoverageProfile, RouteDecision, Router

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


class DeciderStarved(RuntimeError):
    """The decider returned an empty or unparseable reply. This is an outage
    signal — a reasoning model starved of output budget returns an empty string,
    not a refusal — never a genuine 'none': treating it as a decline silently
    declines everything."""


_SYSTEM = (
    "You route a data question to the ONE governed lens that covers it, or decline if none "
    "does. A lens covers a question when the answer lies within its governed metrics OR its "
    "stated purpose/domain — a question that names a lens's subject area is covered even if it "
    "does not name a specific metric (e.g. 'numbers for the board' → the board-reporting lens). "
    "Reply 'none' only when NO lens's metrics or purpose fit; never stretch an unrelated lens. "
    "Reply with ONLY a lens name or the word none."
)


def _catalogue(profiles: list[CoverageProfile]) -> str:
    return "\n".join(
        f"- {p.lens}: governed metrics: {'; '.join(p.anchors)}."
        + (f" ({p.description})" if p.description else "")
        for p in profiles
    )


def _parse(text: str, names: set[str]) -> str | None:
    """A lens name appearing in any form (customer_value / "customer value" / **bolded** /
    "the sales lens"). Longest name first so customer_value beats a stray "customer"."""
    low = text.strip().lower()
    for name in sorted(names, key=len, reverse=True):
        if name in low or name.replace("_", " ") in low:
            return name
    return None


class LlmDecider:
    """Picks the covering lens among shortlisted candidates, or declines. Provider-injected.

    Raises ``DeciderStarved`` on an empty/unparseable reply — only an explicit
    'none' is a decline; everything else is an outage the caller must surface.
    """

    def __init__(self, provider: LLMProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    def decide(self, question: str, candidates: list[CoverageProfile]) -> str | None:
        if not candidates:
            return None
        user = (
            f"Lenses:\n{_catalogue(candidates)}\n\n"
            f"Question: {question}\nAnswer (lens name or none):"
        )
        out = self._provider.complete(
            system=[CacheableBlock(text=_SYSTEM)],
            messages=[Message(role="user", content=user)],
            model=self._model,
            temperature=0.0,
            # deepseek reasons in output tokens; a tight cap truncates before the visible
            # answer lands (and silently declines everything). Give the answer room.
            max_tokens=512,
        )
        reply = out.text or ""
        chosen = _parse(reply, {p.lens for p in candidates})
        if chosen is not None:
            return chosen
        if "none" in reply.lower():
            return None
        raise DeciderStarved(
            f"decider reply named no lens and no 'none' ({len(reply)} chars) — "
            "starvation/garbage, not a decline"
        )


def route_with_llm(
    router: Router,
    profiles_by_lens: dict[str, CoverageProfile],
    decider: LlmDecider,
    question: str,
    *,
    k: int | None = None,
) -> RouteDecision:
    """Cosine shortlist → LLM coverage decision, with ONE deterministic fast-path.

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
    if best_score >= router.confident:
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
        chosen = decider.decide(question, shortlist)
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
    if chosen is None:
        return RouteDecision(False, None, best_score, scored, "no governed lens covers this (llm)")
    score = dict(scored).get(chosen, best_score)
    alternatives = [(lens, s) for lens, s in scored if lens != chosen]
    return RouteDecision(True, chosen, score, alternatives, "routed (llm)", decided_by="decider")
