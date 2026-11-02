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
from dataclasses import replace

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
from services.router.coverage import asked_words, covers, held_by, unused_words, word_keys
from services.router.sibling import (
    by_sources,
    certified_owner,
    distinct_profiles,
    drop_not_for,
    metric_name_owner,
    named_lens,
    named_metrics,
    refusal_owner,
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
    "purpose/domain, and it can split and filter by everything the question asks for "
    "(its dimensions and stored values) — a question that names a lens's subject area is "
    "covered even if it does not name a specific metric (e.g. 'numbers for the board' → "
    "the board-reporting lens). Reply none only when NO lens's metrics or purpose fit; "
    "never stretch an unrelated lens."
)


_SCOPES = {"own": "the organisation's own", "market": "the market's, or other organisations'"}


def _options(profiles: list[CoverageProfile]) -> list[Option]:
    return [
        Option(
            p.lens,
            f"governed metrics: {'; '.join(p.anchors)}."
            + (f" ({p.description})" if p.description else "")
            + (f" Over time by: {', '.join(p.time_fields)}." if p.time_fields else "")
            + (f" Split or filtered by: {', '.join(p.dimensions)}." if p.dimensions else "")
            + (
                " Stored values: "
                + "; ".join(f"{c}: {', '.join(v)}" for c, v in p.values.items())
                + "."
                if p.values
                else ""
            )
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

    def decide_measured(
        self,
        question: str,
        candidates: list[CoverageProfile],
        *,
        catalogue: list[CoverageProfile] | None = None,
    ) -> Decision:
        """``catalogue``: every lens the caller may use. Given, the decision's
        context describes all of them (the same text for every question, so the
        provider caches it) and the options are the candidates' names."""
        decider: Decider | None = getattr(self, "_decider", None)
        if decider is None:
            # A subclass that overrides decide() without a provider (tests' gold
            # deciders): its answer is a choice with nothing measured.
            return Decision(chosen=self.decide(question, candidates), probs=None, provider="stub")
        if not candidates:
            return Decision(chosen=None, probs=None, provider="none-offered")
        context, options = _CONTEXT, _options(candidates)
        if catalogue:
            lines = "\n".join(
                f"- {o.name}: {o.description}"
                for o in _options(sorted(catalogue, key=lambda p: p.lens))
            )
            context = f"{_CONTEXT}\n\nThe lenses:\n{lines}"
            options = [Option(p.lens, "") for p in candidates]
        with ai_usage.purpose("route"):
            return decider.decide(
                question=question, context=context, options=options, allow_none=True
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


# How many lenses, best first, the whole-question coverage check walks. The
# check itself is deterministic and free; what costs is what follows it: each
# covering lens is a fallback the door may type when the one before refuses
# for data it does not hold, and typing a lens costs decider calls. Three
# holds the decider's pick and the two next-ranked lenses (a sibling pair and
# one more), which is where a question passed over by one lens lands; past
# that the ranking is similarity in the compressed noise band, and a lens
# reached only there is a guess, not a candidate.
COVERAGE_K = 3


def _quoted(words: list[str]) -> str:
    return ", ".join(f"'{w}'" for w in words)


def _ranked(
    question: str, candidates: list[CoverageProfile], scored: list[tuple[str, float]]
) -> list[str]:
    """The candidates best first: by similarity, a lens that declares a metric
    the question names (its name or label) ahead of one that does not. A named
    metric is one signal here, never the route: the coverage check still
    passes the lens over when it lacks a word the question asks with."""
    named = {lens for owners in named_metrics(question, candidates).values() for lens in owners}
    score = dict(scored)
    lenses = [p.lens for p in candidates]
    return sorted(lenses, key=lambda n: (n not in named, -score.get(n, -1.0)))


def _walk(
    question: str,
    order: list[str],
    by_lens: dict[str, CoverageProfile],
    held: frozenset[str] | None,
    chain: list[dict[str, str]],
) -> list[str]:
    """The lenses of ``order`` that cover the whole question, in order; each
    one passed over is recorded on ``chain`` with the words it lacks."""
    covering = []
    passed = {c["lens"] for c in chain if c["result"] == "passed over"}
    for lens in order:
        p = by_lens.get(lens)
        if p is None or lens in passed:
            continue
        ok, unused = covers(p, question, held)
        if ok:
            covering.append(lens)
        else:
            chain.append(
                {"lens": lens, "result": "passed over", "why": f"nothing for {_quoted(unused)}"}
            )
    return covering


def _covered_route(
    base: RouteDecision,
    covering: list[str],
    scored: list[tuple[str, float]],
    chain: list[dict[str, str]],
    reason: str = "",
) -> RouteDecision:
    """``base`` sent to the first covering lens, the rest its fallbacks."""
    lens = covering[0]
    chain.append({"lens": lens, "result": "routed", "why": "covers every word of the question"})
    if lens == base.lens and base.covered:
        return replace(base, chain=chain, fallbacks=covering[1:])
    return RouteDecision(
        True,
        lens,
        dict(scored).get(lens, 0.0),
        [(n, s) for n, s in scored if n != lens],
        reason or f"routed (the first lens that covers the whole question; {base.lens} does not)",
        decided_by="coverage",
        decision=base.decision,
        verdict=base.verdict,
        spent=base.spent,
        chain=chain,
        fallbacks=covering[1:],
    )


def _uncovered(
    question: str,
    closest: str,
    by_lens: dict[str, CoverageProfile],
    held: frozenset[str] | None,
    base: RouteDecision,
    chain: list[dict[str, str]],
) -> RouteDecision:
    """No lens near the top covers the whole question: say which words the
    closest lacks. The door then tries the explore scope, or refuses with this."""
    _ok, unused = covers(by_lens[closest], question, held)
    return replace(
        base,
        covered=False,
        lens=None,
        reason=(
            f"no lens covers every word of this question: the closest, {closest}, "
            f"has nothing for {_quoted(unused)}"
        ),
        decided_by="coverage",
        chain=chain,
        fallbacks=[],
    )


def _route_cube(
    question: str,
    ranked: list[str],
    by_lens: dict[str, CoverageProfile],
    held: frozenset[str] | None,
    scored: list[tuple[str, float]],
    router: Router,
    decider: LlmDecider | None,
    catalogue: list[CoverageProfile],
    k: int,
) -> RouteDecision:
    """Routing among Cube-backed lenses: coverage first, the model only on a tie.

    Every candidate is scored by the words of the question some lens
    declares that it lacks (``router.coverage``); the lenses lacking fewest
    are the field. A lens lacking some words is still a candidate when it
    holds at least one of the declared words asked: its lane's own word
    check refuses what it cannot bind, so the router need not decline for
    it. One lens in the field routes with no model asked. Several: a metric
    name only one declares, then the tie rules of ``shared_metric``, then a
    near-verbatim anchor match settle it; otherwise the decider picks among
    them, reading the whole catalogue. The rest, fewest words lacking first,
    are the door's fallbacks."""
    chain: list[dict[str, str]] = []
    lacking = {n: unused_words(question, by_lens[n], held) for n in ranked}
    asked = [w for w in asked_words(question) if held is None or word_keys(w) & held]
    order = sorted(ranked, key=lambda n: len(lacking[n]))  # stable: rank breaks ties
    viable = [n for n in order if not lacking[n] or len(lacking[n]) < len(asked)]
    if not viable:
        closest = ranked[0]
        return RouteDecision(
            False,
            None,
            dict(scored).get(closest, 0.0),
            scored,
            f"no lens holds the words of this question: the closest, {closest}, "
            f"has nothing for {_quoted(lacking[closest])}",
            decided_by="coverage",
            chain=chain,
        )
    fewest = len(lacking[viable[0]])
    # Lenses holding every word are the field. When none does, the field is
    # every lens within one word of the closest: a word met only in another
    # lens's value list or alias separates them too weakly to route on.
    field = [n for n in viable if len(lacking[n]) <= (fewest if not fewest else fewest + 1)]
    for n in order:
        if n not in field:
            chain.append(
                {"lens": n, "result": "passed over", "why": f"nothing for {_quoted(lacking[n])}"}
            )

    def routed(
        lens: str, why: str, decided_by: str, base: RouteDecision | None = None
    ) -> RouteDecision:
        chain.append({"lens": lens, "result": "routed", "why": why})
        return RouteDecision(
            True,
            lens,
            dict(scored).get(lens, 0.0),
            [(n, s) for n, s in scored if n != lens],
            f"routed ({why})",
            decided_by=decided_by,
            decision=base.decision if base else None,
            verdict=base.verdict if base else None,
            spent=base.spent if base else [],
            chain=chain,
            fallbacks=[n for n in viable if n != lens][: max(k - 1, 0)],
        )

    held_all = (
        "covers every word of the question"
        if not fewest
        else (f"holds the most of its words; nothing for {_quoted(lacking[field[0]])}")
    )
    if len(field) == 1:
        return routed(field[0], held_all, "coverage")
    tied = [by_lens[n] for n in field]
    named = metric_name_owner(question, tied)
    if named is not None:
        return routed(named, "names a metric only it declares", "declared-name")
    pick, _ask = shared_metric(question, tied)
    if pick is not None:
        return routed(pick, "the tie among the owners of the metric named", "declared-name")
    named = named_lens(question, field) or unique_name_owner(question, tied)
    if named is not None:
        return routed(named, "the question names what only it declares", "declared-name")
    best = max(field, key=lambda n: dict(scored).get(n, -1.0))
    if dict(scored).get(best, 0.0) >= router.confident:
        return routed(best, "cosine confident", "cosine-verbatim")
    if decider is None:
        return routed(field[0], "the first of the lenses that cover it equally", "cosine")
    shortlist = tied[:_MAX_SHORTLIST]
    try:
        decision = decider.decide_measured(question, shortlist, catalogue=catalogue)
    except Exception as exc:
        log.exception("coverage decider unavailable — declining (degraded)")
        return RouteDecision(
            False,
            None,
            dict(scored).get(best, 0.0),
            scored,
            "routing degraded: the coverage decider is unavailable — declining rather than "
            f"guessing among {', '.join(field)}",
            degraded=DECIDER_DOWN,
            spent=spent_by(exc),
            chain=chain,
        )
    verdict = decision_policy.verdict(decision, "lens")
    base = RouteDecision(
        False, None, 0.0, scored, "", decided_by="decider", decision=decision, verdict=verdict
    )
    if verdict == "clarify":
        ranked_p = sorted((decision.probs or {}).items(), key=lambda kv: -kv[1])
        pair = [n for n, _p in ranked_p[:2] if n != "none"]
        return replace(
            base,
            score=dict(scored).get(best, 0.0),
            reason=(
                f"ambiguous between {pair[0]} and {pair[1]} — name the lens"
                if len(pair) == 2
                else f"unsure whether {pair[0] if pair else 'any lens'} covers this — name the lens"
            ),
            chain=chain,
        )
    if decision.chosen is None or decision.chosen not in field:
        return replace(
            base,
            score=dict(scored).get(best, 0.0),
            reason=f"the decision provider ({decision.provider}) refused to route this"
            if decision.refused
            else "no governed lens covers this (llm)",
            chain=chain,
        )
    return routed(decision.chosen, f"the decider's pick of {', '.join(field)}", "decider", base)


def route_question(
    profiles: list[CoverageProfile],
    embedder: Embedder,
    decider: LlmDecider | None,
    question: str,
    *,
    scorer: Scorer | None = None,
    known_sources: Iterable[str] | None = None,
    k: int = COVERAGE_K,
) -> RouteDecision:
    """The full routing order, deterministic first (``services/router/sibling.py``):
    drop lenses whose ``not_for`` names the ask; route a certified question to its
    one owning lens; keep the lenses declaring every source system the question
    names (``known_sources``: every system a lens in the org declares, default
    the candidates' own; none declaring it is uncovered, saying so); route a
    question naming a measure a lens declares not computable to that lens (or
    where its entry routes it), whose refusal is the answer. Then rank
    the rest by similarity, a lens declaring a metric the question names first,
    and check whole-question coverage (``router.coverage``) on the top ``k``:

    - a metric name or label one lens alone declares routes to it when that
      lens covers the question, with no model asked;
    - a metric several of the top lenses declare and cover is a tie among
      them: a lens named after the asked thing, a declared name only one
      carries, or a split over time only one can make picks it; otherwise the
      question is asked which lens;
    - otherwise the decider picks over its shortlist (pure cosine when none is
      configured), and the route goes to the first lens of [its pick, then the
      ranking] that covers the whole question, the next ones covering it kept
      as fallbacks for the door. None covering is uncovered, naming the words
      the closest lens lacks.

    Every lens tried and why it was passed over rides ``RouteDecision.chain``."""
    held = held_by(profiles)
    profiles = drop_not_for(question, profiles)
    owner = certified_owner(question, profiles)
    if owner is not None:
        return RouteDecision(
            True,
            owner,
            1.0,
            [],
            "routed (certified question)",
            decided_by="certified",
            chain=[
                {"lens": owner, "result": "routed", "why": "the question is its certified question"}
            ],
        )
    known = [s for p in profiles for s in p.sources] if known_sources is None else known_sources
    profiles, uncovered = by_sources(question, profiles, known)
    if uncovered is not None:
        return RouteDecision(False, None, 0.0, [], uncovered, decided_by="declared-name")
    # A measure some lens declares it cannot compute: that lens owns the
    # refusal (its door says why), or hands the question to the lens its
    # entry routes to. No fallbacks: a refusal by declaration is the answer.
    owner = refusal_owner(question, profiles)
    if owner is not None:
        return RouteDecision(
            True,
            owner,
            1.0,
            [],
            "routed (names a measure declared not computable)",
            decided_by="not_computable",
            chain=[
                {
                    "lens": owner,
                    "result": "routed",
                    "why": "names a measure a lens declares it cannot compute",
                }
            ],
        )
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
    by_lens = {p.lens: p for p in profiles}
    chain: list[dict[str, str]] = []
    scored = router.scored(question) if candidates else []
    ranked = _ranked(question, candidates, scored)
    if candidates and all(p.cube_lane for p in candidates):
        return _route_cube(question, ranked, by_lens, held, scored, router, decider, profiles, k)

    named = metric_name_owner(question, candidates)
    if named is not None:
        ok, unused = covers(by_lens[named], question, held)
        if ok:
            route = _named_route(named, scored)
            others = [n for n in ranked[:k] if n != named]
            fallbacks = _walk(question, others, by_lens, held, [])
            chain.append(
                {"lens": named, "result": "routed", "why": "names a metric only it declares"}
            )
            return replace(route, chain=chain, fallbacks=fallbacks)
        chain.append(
            {
                "lens": named,
                "result": "passed over",
                "why": f"names a metric only it declares, but nothing for {_quoted(unused)}",
            }
        )

    # A metric several lenses declare: a tie among the top lenses that cover
    # the question, never among every lens the caller may use.
    tied = [by_lens[n] for n in _walk(question, ranked[:k], by_lens, held, [])]
    pick, ask = shared_metric(question, tied)
    if pick is not None:
        chain.append(
            {
                "lens": pick,
                "result": "routed",
                "why": "the tie among the owners of the metric named",
            }
        )
        return replace(_named_route(pick, scored), chain=chain)
    if ask is not None:
        return RouteDecision(
            False,
            None,
            scored[0][1] if scored else 0.0,
            scored,
            ask,
            decided_by="declared-name",
            verdict="clarify",
            chain=chain,
        )

    if decider is None or not scoring:
        base = router.route(question)
    else:
        # The decider reads each lens whole: the anchors a sibling shares are
        # left out of SCORING only (they separate nothing there), never out of
        # what the decider is told a lens covers.
        base = route_with_llm(router, {p.lens: p for p in profiles}, decider, question)
    if base.degraded:
        return base
    if base.covered and base.lens is not None:
        order = list(dict.fromkeys([base.lens, *ranked]))[:k]
        covering = _walk(question, order, by_lens, held, chain)
        if covering:
            return _covered_route(base, covering, scored, chain)
        return _uncovered(question, base.lens, by_lens, held, base, chain)
    if base.verdict == "clarify" and base.decision is not None and base.decision.probs:
        # Two lenses close together: coverage, then a lens named after the
        # asked thing, settles it; otherwise the question is asked.
        top = sorted(base.decision.probs.items(), key=lambda kv: -kv[1])
        pair = [n for n, _p in top[:2] if n in by_lens]
        covering = _walk(question, pair, by_lens, held, chain)
        if len(covering) == 2:
            covering = [n for n in [named_lens(question, covering)] if n] or covering
        if len(covering) == 1:
            return _covered_route(
                base,
                covering,
                scored,
                chain,
                f"routed (of {' and '.join(pair)}, which the decider could not separate, "
                f"{covering[0]} covers the whole question)",
            )
    return replace(base, chain=chain)
