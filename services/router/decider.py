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
from collections.abc import Iterable

from services.contracts.protocols import Decider, Decision, LLMProvider, Option, spent_by
from services.llm.vote_decider import DEFAULT_K, DeciderStarved, VoteDecider
from services.observability import usage as ai_usage
from services.router import (
    DECIDER_DOWN,
    CoverageProfile,
    Embedder,
    RouteDecision,
    Router,
    Scorer,
)
from services.router.sibling import (
    by_sources,
    certified_owner,
    distinct_profiles,
    drop_not_for,
    metric_name_owner,
    shared_metric,
    unique_name_owner,
)
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


__all__ = ["DeciderStarved", "LlmDecider", "route_question", "route_with_llm", "shortlist_k"]

_CONTEXT = (
    "Route a data question to the ONE governed lens that covers it, or none. A lens covers "
    "a question when the answer lies within its governed metrics OR its stated "
    "purpose/domain — a question that names a lens's subject area is covered even if it "
    "does not name a specific metric (e.g. 'numbers for the board' → the board-reporting "
    "lens). Reply none only when NO lens's metrics or purpose fit; never stretch an "
    "unrelated lens."
)


_SCOPES = {"own": "the organisation's own", "market": "the market's, or other organisations'"}


def _options(profiles: list[CoverageProfile]) -> list[Option]:
    return [
        Option(
            p.lens,
            f"governed metrics: {'; '.join(p.anchors)}."
            + (f" ({p.description})" if p.description else "")
            + (f" Over time by: {', '.join(p.time_fields)}." if p.time_fields else "")
            + (f" Reads: {', '.join(p.sources)}." if p.sources else "")
            + (f" Figures: {_SCOPES[p.data_scope]}." if p.data_scope in _SCOPES else "")
            + (f" Not for: {'; '.join(p.not_for)}." if p.not_for else ""),
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
        with ai_usage.purpose("route"):
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
    unscored = _unscored(router, profiles_by_lens, scored, question)
    if not scored and not unscored:
        return RouteDecision(False, None, 0.0, [], "no lenses to route to")
    best_lens, best_score = scored[0] if scored else ("", 0.0)
    if all_lenses is None:
        all_lenses = bool(getattr(decider, "typed_decisions", False))
    if all_lenses:
        k = len(scored) + len(unscored)
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
        k = shortlist_k(len(scored) + len(unscored))
    shortlist = [profiles_by_lens[lens] for lens, _ in scored[:k] if lens in profiles_by_lens]
    shortlist += _siblings_of(shortlist, unscored, alone=all_lenses or not scored)
    if len(shortlist) == 2:
        named = unique_name_owner(question, shortlist)
        if named is not None:
            return _named_route(named, scored)
    try:
        with ai_usage.purpose("route"):
            decision = (
                decider.decide_measured(question, shortlist)
                if hasattr(decider, "decide_measured")
                else Decision(
                    chosen=decider.decide(question, shortlist), probs=None, provider="stub"
                )
            )
    except Exception as exc:
        log.exception("coverage decider unavailable — declining (degraded), never mid-band cosine")
        return RouteDecision(
            False,
            None,
            best_score,
            scored,
            "routing degraded: the coverage decider is unavailable — declining rather than "
            "guessing from the ambiguous cosine band",
            degraded=DECIDER_DOWN,
            spent=spent_by(exc),
        )
    verdict = decision_policy.verdict(decision, "lens")
    if verdict == "clarify":
        # Measured, and not sure enough to act: an AMBIGUITY, which is a
        # coverage signal (name the lens), never an outage — no degraded mark.
        ranked = sorted((decision.probs or {}).items(), key=lambda kv: -kv[1])
        lenses = [k for k, _v in ranked[:2] if k != "none"]
        if len(lenses) == 2:
            named = unique_name_owner(
                question, [profiles_by_lens[n] for n in lenses if n in profiles_by_lens]
            )
            if named is not None:
                return _named_route(named, scored, decision)
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
            # A refusal is the provider's, never a coverage finding.
            f"the decision provider ({decision.provider}) refused to route this"
            if decision.refused
            else "no governed lens covers this (llm)",
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


def _unscored(
    router: Router,
    profiles_by_lens: dict[str, CoverageProfile],
    scored: list[tuple[str, float]],
    question: str,
) -> list[CoverageProfile]:
    """Candidates similarity did not rank and no ``not_for`` excludes: a lens
    whose every anchor a sibling carries too (``distinct_profiles`` leaves it
    none to score)."""
    ranked = {lens for lens, _ in scored}
    declined = router.not_for(question)
    return [
        p
        for lens, p in profiles_by_lens.items()
        if lens not in ranked and lens not in declined and p.anchors
    ]


def _siblings_of(
    shortlist: list[CoverageProfile], unscored: list[CoverageProfile], *, alone: bool
) -> list[CoverageProfile]:
    """The unscored lenses that carry an anchor a shortlisted lens carries: the
    text describes both equally, so similarity cannot rank one and the decider
    must see both. When nothing scored at all, every such lens is a sibling of
    another, and all of them are the shortlist (as they are when the decider
    sees every lens)."""
    if alone:
        return unscored
    shown = {a for p in shortlist for a in p.anchors}
    return [p for p in unscored if shown & set(p.anchors)]


def _named_route(
    lens: str, scored: list[tuple[str, float]], decision: Decision | None = None
) -> RouteDecision:
    return RouteDecision(
        True,
        lens,
        dict(scored).get(lens, 0.0),
        [(n, s) for n, s in scored if n != lens],
        "routed (the question names what only this lens declares)",
        decided_by="declared-name",
        decision=decision,
    )


def route_question(
    profiles: list[CoverageProfile],
    embedder: Embedder,
    decider: LlmDecider | None,
    question: str,
    *,
    scorer: Scorer | None = None,
    known_sources: Iterable[str] | None = None,
) -> RouteDecision:
    """The full routing order, deterministic first (``services/router/sibling.py``):
    drop lenses whose ``not_for`` names the ask; route a certified question to its
    one owning lens; keep the lenses declaring every source system the question
    names (``known_sources``: every system a lens in the org declares, default
    the candidates' own; none declaring it is uncovered, saying so); route a
    metric name or label one lens alone declares to it (never to a lens whose
    ``not_for`` the question is nearer than its anchors); a metric several
    lenses declare goes to the one a declared name or a time split picks, else
    the question is asked which lens; score only the anchors a lens does not
    share verbatim; then the decider over the shortlist (pure cosine when none
    is configured)."""
    profiles = drop_not_for(question, profiles)
    owner = certified_owner(question, profiles)
    if owner is not None:
        return RouteDecision(
            True, owner, 1.0, [], "routed (certified question)", decided_by="certified"
        )
    known = [s for p in profiles for s in p.sources] if known_sources is None else known_sources
    profiles, uncovered = by_sources(question, profiles, known)
    if uncovered is not None:
        return RouteDecision(False, None, 0.0, [], uncovered, decided_by="declared-name")
    scoring = distinct_profiles(profiles)
    if scorer is not None:
        # A stored scorer was built over every accessible lens: keep its ranking
        # to the lenses still in play.
        keep = {p.lens for p in scoring}
        inner = scorer

        def scorer(qv: list[float]) -> list[tuple[str, float]]:
            return [(n, s) for n, s in inner(qv) if n in keep]

    router = Router(scoring, embedder, scorer=scorer)
    declined = router.not_for(question)
    candidates = [p for p in profiles if p.lens not in declined]
    named = metric_name_owner(question, candidates)
    if named is not None:
        return _named_route(named, router.scored(question))
    named, ask = shared_metric(question, candidates)
    if named is not None:
        return _named_route(named, router.scored(question))
    if ask is not None:
        scored = router.scored(question)
        return RouteDecision(
            False,
            None,
            scored[0][1] if scored else 0.0,
            scored,
            ask,
            decided_by="declared-name",
            verdict="clarify",
        )
    if decider is None or not scoring:
        return router.route(question)
    # The decider reads each lens whole: the anchors a sibling shares are
    # left out of SCORING only (they separate nothing there), never out of
    # what the decider is told a lens covers.
    return route_with_llm(router, {p.lens: p for p in profiles}, decider, question)
