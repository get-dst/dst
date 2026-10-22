"""Lens router — a managed lens-kind that routes a question to the covering lens.

The router's *source* is the lens catalogue and its *answer* is a route. It is
auto-derived from the published lenses' coverage profiles and **owned by dst,
not configured by the customer**. A question routes to the best-covering
lens above a dst-tuned threshold, or it DECLINES — and the decline is the
honest surface-area signal (no ground truth needed): "no governed lens covers this."

This module is the routing engine: pure, embedder-injected, offline-testable. The
"just ask" data-plane/MCP surface and the surface-area rollup are separate WIs.

Decline calibration
-----------------------------
Embedding cosines compress into a high band: a question NO lens governs can still
score well clear of any absolute threshold low enough to route real questions, so
a naive single threshold over-routes and *never* declines — which would make
surface area falsely read ~100%. Two changes fix it, tuned against the held-out
routing eval (``services/router/eval.py``):

  1. **Specific anchors, not the broad description.** We match against the lens's
     governed-metric / sample-question anchors only. The broad
     ``"<lens>: <description>"`` anchor matches almost anything, so it is recorded
     for *display* (``CoverageProfile.description``) but kept OUT of the scoring
     set — it was the single biggest cause of false routes.
  2. **A floor AND a margin, not one absolute threshold.** Route only when the
     best lens both (a) clears a calibrated ``threshold`` floor and (b) either
     clears a higher ``confident`` bar outright OR beats the runner-up lens by
     ``margin``. An uncovered question lands in the compressed noise band where
     several lenses score similarly — the margin collapses and it DECLINES. A
     covered question matches one lens's specific anchor strongly and pulls away,
     so it routes. The margin is what makes the decline honest in a compressed
     cosine range; the values are tuned in ``eval.py`` (see ``tune_threshold`` and
     ``SHIPPED`` there).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from services.contracts.protocols import Decision, DecisionUsage
from services.contracts.protocols import Embedder as Embedder
from services.runtime.not_for import names_ask

# A pre-scored recall signal: the question's vector -> (lens, score) ranked best
# first. Injected when anchor vectors live elsewhere (pgvector) so the Router
# never re-embeds anchors at request time; None keeps the in-memory embedding
# path. The Router embeds the question once and hands the vector over.
Scorer = Callable[[list[float]], list[tuple[str, float]]]

# dst-owned defaults, tuned against the routing eval — never a user
# knob. See services/router/eval.py for the grid search that picked these and the
# precision/recall they buy on the held-out labeled set.
#
#   DEFAULT_THRESHOLD — the floor: below this, always decline (compressed-range
#       noise lives here; nothing routes from it).
#   DEFAULT_CONFIDENT — a strong, unambiguous match routes on its own, no margin
#       needed (a near-exact governed-metric hit).
#   DEFAULT_MARGIN    — between floor and confident, route ONLY if the best lens
#       beats the runner-up by this much. This is the decline lever: an uncovered
#       question scores similarly against several lenses (small margin → decline);
#       a covered one pulls away from the field (large margin → route).
DEFAULT_THRESHOLD = 0.78
DEFAULT_CONFIDENT = 0.95
DEFAULT_MARGIN = 0.07

# Degraded-routing markers: stamped on the decline envelope and the
# routing_decision row so an outage is visible as an outage — never a silent
# fallback to the mid-band cosine route, and never
# counted as a coverage decline on the surface report.
DECIDER_DOWN = "DECIDER_DOWN"  # the LLM coverage decider errored or starved
ROUTER_DOWN = "ROUTER_DOWN"  # routing machinery itself failed (embedder, store)


@dataclass
class CoverageProfile:
    """What a lens governs, as a matchable surface — the router's view of one lens.

    ``anchors`` is the SCORING set: specific governed metrics, definitions, and
    sample questions. ``description`` is the broad ``"<lens>: <summary>"`` blurb —
    kept for display/diagnostics only and deliberately NOT scored, because a broad
    description matches almost any question (the chief over-routing cause).
    """

    lens: str
    anchors: list[str]  # SPECIFIC governed metrics / definitions / sample questions
    scope: frozenset[str] = field(default_factory=frozenset)
    description: str = ""  # broad lens blurb — display only, never scored
    # The lens's certified questions (template questions at their sample
    # rendering): a question equal to one of them routes here without scoring.
    certified: list[str] = field(default_factory=list)
    # Declared names (tables, governed entities, metrics) for the word-boundary
    # tie-break between two sibling candidates.
    names: list[str] = field(default_factory=list)
    # Asks the lens declares it is not for: such a question never routes here.
    not_for: list[str] = field(default_factory=list)
    # What its metrics are called: name and label. A question naming one only
    # this lens declares routes here ahead of similarity. Aliases are left
    # out: they widen what the metric slot reads inside the lens, never which
    # questions the lens takes.
    metric_names: list[str] = field(default_factory=list)
    # The fields its figures split over time by (entity default, metric
    # overrides): the decider reads them, so a trend ask finds a trend lens.
    time_fields: list[str] = field(default_factory=list)
    # The source systems the lens declares it reads (lens.yaml ``sources``):
    # a question naming one goes only to the lenses declaring it.
    sources: list[str] = field(default_factory=list)
    # Whose figures it holds (lens.yaml ``scope``): "own", "market" or "".
    data_scope: str = ""


@dataclass
class RouteDecision:
    covered: bool
    lens: str | None
    # Anchor-cosine of the chosen lens — a RETRIEVAL signal, never a decision
    # confidence: cosine ranks misses above hits often enough that no threshold
    # separates them, which is WHY the decider decides and this number only
    # shortlists. Published to callers with that framing, never for thresholds.
    score: float
    alternatives: list[tuple[str, float]]  # (lens, score), ranked, best first
    reason: str = ""
    # What actually made the call: "certified" (the question is a lens's
    # certified question), "declared-name" (it names a table, entity or metric
    # only one of two candidates declares), "decider" (LLM coverage decision),
    # "cosine-verbatim" (the near-exact fast path), or "cosine" (no decider
    # configured — pure similarity, the weakest basis).
    decided_by: str = "cosine"
    # Set when the decline is an OUTAGE, not a coverage signal (DECIDER_DOWN /
    # ROUTER_DOWN) — rides the envelope and the routing_decision row.
    degraded: str | None = None
    # The decider's measured decision (contracts.protocols.Decision) and the
    # policy verdict it got — act | clarify | decline. None on the cosine paths
    # and on outages. Rides the routing_decision row so calibration and the
    # act/clarify split are queries, not archaeology.
    decision: Decision | None = None
    verdict: str | None = None
    # What the decider billed before an outage cut it short (some of its
    # votes answered, a reply came back unreadable), as ``(provider, usage)``
    # pairs: no decision was made, but the spend is recorded on the row.
    spent: list[tuple[str, DecisionUsage]] = field(default_factory=list)


# (embedder model, dim, ask) -> vector: the not_for asks, embedded once per process.
_ASK_CACHE: dict[tuple[str, int, str], list[float]] = {}
_ASK_CACHE_MAX = 4096


def _ask_vectors(embedder: Embedder, asks: list[str]) -> list[list[float]]:
    ident = (str(getattr(embedder, "model", type(embedder).__name__)), int(embedder.dim))
    missing = [a for a in dict.fromkeys(asks) if (*ident, a) not in _ASK_CACHE]
    if missing:
        if len(_ASK_CACHE) + len(missing) > _ASK_CACHE_MAX:
            _ASK_CACHE.clear()
        for ask, vec in zip(missing, embedder.embed(missing), strict=True):
            _ASK_CACHE[(*ident, ask)] = vec
    return [_ASK_CACHE[(*ident, a)] for a in asks]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Router:
    """Routes a question to its covering lens, or declines. Constructed from coverage
    profiles + an embedder; dst owns the threshold/margin (no per-customer config).

    Decline = the best lens fails the floor, or sits in the compressed noise band
    without separating from the runner-up. See the module docstring + eval.py.
    """

    def __init__(
        self,
        profiles: list[CoverageProfile],
        embedder: Embedder,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        confident: float = DEFAULT_CONFIDENT,
        margin: float = DEFAULT_MARGIN,
        scorer: Scorer | None = None,
    ) -> None:
        self._embedder = embedder
        self._threshold = threshold
        self._confident = confident
        self._margin = margin
        self._scorer = scorer
        self._has_profiles = bool(profiles)
        # The asks each lens declares it is not for, as text (the literal
        # match) and as vectors (the similarity match). Few and authored, so
        # embedded once per process and embedder (``_ask_vectors``).
        self._not_for = {p.lens: list(p.not_for) for p in profiles if p.not_for}
        self._not_for_vecs = {
            lens: _ask_vectors(embedder, asks) for lens, asks in self._not_for.items()
        }
        # The last question embedded and ranked: the deterministic routes ask
        # before the decider does, and neither pays for it twice.
        self._last: tuple[str, list[float]] | None = None
        self._ranked: tuple[str, list[tuple[str, float]]] | None = None
        # Score ONLY the specific anchors — the broad description is display-only.
        # With an injected scorer the vectors live elsewhere (publish-time pgvector
        # rows): embed NOTHING here — the request path embeds only the question.
        flat = [] if scorer is not None else [(p.lens, a) for p in profiles for a in p.anchors]
        self._lens_vecs: dict[str, list[list[float]]] = {}
        if flat:
            vecs = embedder.embed([a for _, a in flat])
            for (lens, _), vec in zip(flat, vecs, strict=True):
                self._lens_vecs.setdefault(lens, []).append(vec)

    # dst-owned thresholds, exposed read-only so the LLM decider can reuse the
    # SAME deterministic gate (confident / floor+margin) instead of hardcoding it.
    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def confident(self) -> float:
        return self._confident

    @property
    def margin(self) -> float:
        return self._margin

    def scored(self, question: str) -> list[tuple[str, float]]:
        """Every lens ranked by cosine to the question (best first), no gating. This is
        the cheap RECALL signal — exact for a near-verbatim match, but unreliable in the
        compressed mid-band (an LLM decider reranks the top-k there; see router/decider.py).

        A lens that declares it is not for this ask is never ranked: the question
        names every content word of one of its ``not_for`` asks (the literal match
        its own door refuses on), or sits nearer one of those asks than to any
        anchor the lens covers (the same similarity that ranks it)."""
        if self._scorer is not None:
            if not self._has_profiles:
                return []
        elif not self._lens_vecs:
            return []
        if self._ranked is not None and self._ranked[0] == question:
            return list(self._ranked[1])
        qv = self._question_vec(question)
        if self._scorer is not None:
            ranked = self._scorer(qv)
        else:
            ranked = sorted(
                (
                    (lens, max(_cosine(qv, v) for v in vecs))
                    for lens, vecs in self._lens_vecs.items()
                ),
                key=lambda x: x[1],
                reverse=True,
            )
        kept = [(lens, s) for lens, s in ranked if not self._declined(lens, s, question, qv)]
        self._ranked = (question, kept)
        return list(kept)

    def not_for(self, question: str) -> set[str]:
        """The lenses that declare they are not for this question, by the rule
        ``scored`` drops them on — for the deterministic routes taken before
        any scoring (a declared name). A lens with no anchors of its own has
        nothing nearer than its asks."""
        if not self._not_for:
            return set()
        qv = self._question_vec(question)
        ranked = dict(self.scored(question))
        return {
            lens
            for lens in self._not_for
            if lens not in ranked and self._declined(lens, 0.0, question, qv)
        }

    def _question_vec(self, question: str) -> list[float]:
        if self._last is None or self._last[0] != question:
            self._last = (question, self._embedder.embed([question])[0])
        return self._last[1]

    def _declined(self, lens: str, score: float, question: str, qv: list[float]) -> bool:
        asks = self._not_for.get(lens)
        if not asks:
            return False
        if any(names_ask(question, a) for a in asks):
            return True
        return max(_cosine(qv, v) for v in self._not_for_vecs[lens]) > score

    def route(self, question: str) -> RouteDecision:
        scored = self.scored(question)
        if not scored:
            return RouteDecision(False, None, 0.0, [], "no lenses to route to")
        best_lens, best_score = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        gap = best_score - runner_up

        # The floor: compressed-range noise lives below it — never route from there.
        if best_score < self._threshold:
            return RouteDecision(
                False,
                None,
                best_score,
                scored,
                f"no governed lens covers this (best {best_score:.2f} "
                f"< floor {self._threshold:.2f})",
            )
        # Above the floor: route on a confident match OR a clear margin over the
        # field. An uncovered question scores similarly against several lenses, so
        # the margin collapses and it declines — THIS is the surface-area signal.
        if best_score >= self._confident or gap >= self._margin:
            return RouteDecision(True, best_lens, best_score, scored[1:], "routed")
        return RouteDecision(
            False,
            None,
            best_score,
            scored,
            f"no governed lens covers this (best {best_score:.2f} clears the floor "
            f"but only beats the runner-up by {gap:.2f} < margin {self._margin:.2f} "
            "— ambiguous, declining)",
        )
