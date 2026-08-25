"""Routing-quality eval — the router gets measured, and the threshold is TUNED to
the number, not guessed.

A small **held-out, contamination-guarded** labeled set: each question is labeled
with the lens that should answer it, or ``None`` for *uncovered* (no lens governs
it — the correct outcome is a DECLINE). We run the router over the set and report:

  * **routing precision** — of the questions we routed, the share routed to the
    right lens. (A mis-route — routed somewhere, but wrong/uncovered — is the
    false positive.)
  * **routing recall** — of the questions a lens *should* answer, the share we
    routed correctly. (An over-decline — declined a covered question — drops it.)
  * **mis-routes** — routed to the wrong lens (or routed an uncovered question).
  * **over-declines** — declined a question a lens could have answered.
  * **correct declines** — declined an uncovered question (the right call — this
    is surface area working).

Contamination guard (reuses ``services/benchmark/contamination.py``): the eval's
TEST questions must not be verbatim among the anchors that *taught* the lenses —
otherwise we'd be measuring lookup, not routing. ``assert_eval_held_out`` refuses
to score a set that leaks, exactly as the benchmark refuses to score a question
the certified teaches.

Tuning: ``tune_threshold`` grid-searches (floor, margin, confident) against this
eval and returns the setting that maximizes correctness then precision. The values
baked into ``services.router`` (``DEFAULT_THRESHOLD`` / ``_MARGIN`` / ``_CONFIDENT``)
are what that search selects on the offline HashEmbedder fixture; the *same shape*
of rule is what the live Voyage run needs (compressed cosines → a single absolute
threshold can't separate covered from uncovered; a floor + a margin can).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from services.benchmark.contamination import assert_held_out, taught_set
from services.router import (
    DEFAULT_CONFIDENT,
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    CoverageProfile,
    Embedder,
    Router,
)

# A labeled question: the lens that should answer it, or None ⇒ uncovered (decline).
LabeledQuestion = tuple[str, str | None]


@dataclass
class RoutingMetrics:
    """Precision/recall + the two router error modes, over a labeled held-out set."""

    routed_correct: list[str] = field(default_factory=list)  # routed to the right lens
    mis_routed: list[str] = field(default_factory=list)  # routed to the WRONG lens
    routed_uncovered: list[str] = field(default_factory=list)  # routed an uncovered q
    over_declined: list[str] = field(default_factory=list)  # declined a covered q
    correct_declined: list[str] = field(default_factory=list)  # declined an uncovered q

    @property
    def total(self) -> int:
        return (
            len(self.routed_correct)
            + len(self.mis_routed)
            + len(self.routed_uncovered)
            + len(self.over_declined)
            + len(self.correct_declined)
        )

    @property
    def mis_routes(self) -> int:
        """Routed somewhere wrong: wrong lens, or routed an uncovered question."""
        return len(self.mis_routed) + len(self.routed_uncovered)

    @property
    def over_declines(self) -> int:
        return len(self.over_declined)

    @property
    def precision(self) -> float | None:
        """Of the questions we routed, the share routed correctly."""
        routed = len(self.routed_correct) + self.mis_routes
        return len(self.routed_correct) / routed if routed else None

    @property
    def recall(self) -> float | None:
        """Of the questions a lens should answer, the share routed correctly."""
        covered = len(self.routed_correct) + len(self.mis_routed) + len(self.over_declined)
        return len(self.routed_correct) / covered if covered else None

    @property
    def correct(self) -> int:
        """Every question handled correctly: right route OR a justified decline."""
        return len(self.routed_correct) + len(self.correct_declined)

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.total if self.total else None

    def render(self) -> str:
        def pct(x: float | None) -> str:
            return f"{x:.0%}" if x is not None else "—"

        return "\n".join(
            [
                "ROUTING-QUALITY (held-out, contamination-guarded)",
                f"  precision (routed → right lens):   {pct(self.precision)}",
                f"  recall (covered → routed):         {pct(self.recall)}",
                f"  accuracy (routes + just declines): "
                f"{self.correct}/{self.total} = {pct(self.accuracy)}",
                f"  mis-routes:        {self.mis_routes}  "
                f"(wrong lens {len(self.mis_routed)} + uncovered-routed "
                f"{len(self.routed_uncovered)})",
                f"  over-declines:     {self.over_declines}  "
                "(a lens could have answered, we declined)",
                f"  correct-declines:  {len(self.correct_declined)}  "
                "(uncovered → declined — surface area working)",
            ]
        )


def evaluate(router: Router, labeled: Iterable[LabeledQuestion]) -> RoutingMetrics:
    """Run ``router`` over a labeled set and tally precision/recall + error modes."""
    m = RoutingMetrics()
    for question, gold in labeled:
        d = router.route(question)
        if d.covered:
            if gold is None:
                m.routed_uncovered.append(question)  # should have declined
            elif d.lens == gold:
                m.routed_correct.append(question)
            else:
                m.mis_routed.append(question)
        else:  # declined
            if gold is None:
                m.correct_declined.append(question)  # the right call
            else:
                m.over_declined.append(question)
    return m


def anchor_corpus(profiles: Iterable[CoverageProfile]) -> list[str]:
    """The text that TEACHES the router — every scored anchor across all lenses.

    The eval's test questions must stay out of this (contamination guard). The
    broad ``description`` is excluded on purpose: it is display-only, never scored,
    so it is not part of what taught the router.
    """
    return [a for p in profiles for a in p.anchors]


def assert_eval_held_out(
    labeled: Iterable[LabeledQuestion], profiles: Iterable[CoverageProfile]
) -> None:
    """Refuse to run the eval if any test question is verbatim in the anchors that
    taught the lenses — that would measure lookup, not routing. Reuses the
    benchmark's exact-overlap detector."""
    taught = taught_set(anchor_corpus(profiles))
    questions = [q for q, _ in labeled]
    assert_held_out(questions, taught)


@dataclass
class TunedSetting:
    threshold: float
    margin: float
    confident: float
    metrics: RoutingMetrics

    def render(self) -> str:
        return (
            f"floor={self.threshold:.2f} margin={self.margin:.2f} "
            f"confident={self.confident:.2f}\n{self.metrics.render()}"
        )


def tune_threshold(
    profiles: list[CoverageProfile],
    embedder: Embedder,
    labeled: Iterable[LabeledQuestion],
    *,
    floors: Iterable[float] = (0.62, 0.70, 0.74, 0.78, 0.82, 0.86),
    margins: Iterable[float] = (0.0, 0.03, 0.05, 0.07, 0.10),
    confidents: Iterable[float] = (0.95,),
) -> TunedSetting:
    """Grid-search (floor, margin, confident) against the eval; pick the setting
    that maximizes correctness, then precision, then recall. This is how the
    dst-owned defaults are TUNED to the number rather than guessed.

    Note ``margin=0`` reduces to the old single-absolute-threshold behaviour — so
    if a plain threshold could separate covered from uncovered the search would
    find it; that it picks a non-zero margin is the evidence the margin is needed.
    """
    labeled = list(labeled)
    best: TunedSetting | None = None
    for floor in floors:
        for margin in margins:
            for confident in confidents:
                router = Router(
                    profiles,
                    embedder,
                    threshold=floor,
                    margin=margin,
                    confident=confident,
                )
                m = evaluate(router, labeled)
                cand = TunedSetting(floor, margin, confident, m)
                if best is None or _better(cand, best):
                    best = cand
    assert best is not None  # labeled grids are never empty in practice
    return best


def _better(a: TunedSetting, b: TunedSetting) -> bool:
    """Order tuning candidates: more correct, then more precise, then more recall,
    then a tighter floor (cheaper/stricter) breaks remaining ties."""
    ka = (
        a.metrics.correct,
        a.metrics.precision or 0.0,
        a.metrics.recall or 0.0,
        a.threshold,
    )
    kb = (
        b.metrics.correct,
        b.metrics.precision or 0.0,
        b.metrics.recall or 0.0,
        b.threshold,
    )
    return ka > kb


# The eval the offline tests run + the live Voyage finding motivates. The exact
# defaults shipped in ``services.router`` are what ``tune_threshold`` selects on
# the HashEmbedder fixture used in tests/test_router_eval.py.
SHIPPED = (DEFAULT_THRESHOLD, DEFAULT_MARGIN, DEFAULT_CONFIDENT)
