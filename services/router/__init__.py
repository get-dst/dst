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

from services.contracts.protocols import Embedder as Embedder

# A pre-scored recall signal: question -> (lens, score) ranked best first. Injected
# when anchor vectors live elsewhere (pgvector) so the Router never
# re-embeds anchors at request time; None keeps the in-memory embedding path.
Scorer = Callable[[str], list[tuple[str, float]]]

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
    # What actually made the call: "decider" (LLM coverage decision),
    # "cosine-verbatim" (the near-exact fast path), or "cosine" (no decider
    # configured — pure similarity, the weakest basis).
    decided_by: str = "cosine"
    # Set when the decline is an OUTAGE, not a coverage signal (DECIDER_DOWN /
    # ROUTER_DOWN) — rides the envelope and the routing_decision row.
    degraded: str | None = None


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
        compressed mid-band (an LLM decider reranks the top-k there; see router/decider.py)."""
        if self._scorer is not None:
            return self._scorer(question) if self._has_profiles else []
        if not self._lens_vecs:
            return []
        qv = self._embedder.embed([question])[0]
        return sorted(
            ((lens, max(_cosine(qv, v) for v in vecs)) for lens, vecs in self._lens_vecs.items()),
            key=lambda x: x[1],
            reverse=True,
        )

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
