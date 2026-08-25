"""Routing quality + decline calibration (offline, HashEmbedder).

HashEmbedder: identical text → cosine 1.0; different text → an unstructured ~0.64–0.87
NOISE band (no semantic transfer). That noise band is a faithful stand-in for
live embeddings, whose cosines compress into ~0.7–1.0, so an uncovered question
can out-score the floor. These tests pin that:

  * a covered question (verbatim a governed-metric anchor → 1.0) ROUTES;
  * an uncovered question (support tickets / weather / HR) DECLINES — and would
    OVER-ROUTE under the old single-absolute-threshold rule, so the margin is what
    earns the decline;
  * the eval computes precision/recall + over-decline counts;
  * the contamination guard keeps the eval's test questions out of the anchors
    that taught the lenses.
"""

from __future__ import annotations

import pytest

from services.benchmark.contamination import ContaminationError, train_test_split
from services.contracts.fakes import HashEmbedder
from services.router import (
    DEFAULT_CONFIDENT,
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    CoverageProfile,
    Router,
)
from services.router.eval import (
    anchor_corpus,
    assert_eval_held_out,
    evaluate,
    tune_threshold,
)

# A small catalogue with SPECIFIC governed-metric anchors (the scored set) and a
# broad description (display only). Covered eval questions are verbatim anchors so
# HashEmbedder gives them 1.0; uncovered ones fall in the noise band.
PROFILES = [
    CoverageProfile(
        lens="finance",
        anchors=[
            "total net invoiced revenue",
            "outstanding receivables",
            "overdue invoices",
            "accounts receivable aging",
        ],
        description="Finance: governed answers over invoices, receivables, payments.",
    ),
    CoverageProfile(
        lens="customer_value",
        anchors=[
            "how many repeat customers",
            "customer lifetime value",
            "average order value",
            "number of orders per customer",
        ],
        description="Customer Value: lifetime value and order activity.",
    ),
    CoverageProfile(
        lens="sales",
        anchors=[
            "quote win rate",
            "which rep won the most deals",
            "pipeline coverage",
            "deals closed this quarter",
        ],
        description="Sales: deals, quotes, pipeline.",
    ),
]

# Held-out labeled set: covered (lens) + uncovered (None ⇒ correct outcome is DECLINE).
LABELED: list[tuple[str, str | None]] = [
    ("total net invoiced revenue", "finance"),
    ("outstanding receivables", "finance"),
    ("how many repeat customers", "customer_value"),
    ("customer lifetime value", "customer_value"),
    ("quote win rate", "sales"),
    ("which rep won the most deals", "sales"),
    # uncovered — no lens governs these; the honest outcome is a decline (surface area)
    ("how many support tickets are open this week", None),
    ("what is the weather tomorrow", None),
    ("headcount in HR", None),
    ("what is our office wifi password", None),
]


def test_covered_question_routes_to_its_lens():
    router = Router(PROFILES, HashEmbedder())  # SHIPPED defaults
    d = router.route("total net invoiced revenue")
    assert d.covered and d.lens == "finance"
    d2 = router.route("how many repeat customers")
    assert d2.covered and d2.lens == "customer_value"


def test_uncovered_question_declines_the_support_tickets_case():
    # A question that mis-routes to finance on cosine alone, at 0.91.
    router = Router(PROFILES, HashEmbedder())
    d = router.route("how many support tickets are open this week")
    assert not d.covered and d.lens is None
    assert "no governed lens covers this" in d.reason
    # weather + HR likewise decline (no lens governs them)
    assert not router.route("what is the weather tomorrow").covered
    assert not router.route("headcount in HR").covered


def test_margin_is_load_bearing_old_absolute_threshold_over_routes():
    # At the SHIPPED floor but margin=0 (the old single-absolute-threshold rule),
    # uncovered questions in the compressed noise band OVER-ROUTE — exactly the
    # compression failure. The margin turns the over-route into a decline.
    floor_only = Router(PROFILES, HashEmbedder(), threshold=DEFAULT_THRESHOLD, margin=0.0)
    with_margin = Router(PROFILES, HashEmbedder())  # shipped margin
    for q in (
        "how many support tickets are open this week",
        "what is the weather tomorrow",
        "headcount in HR",
    ):
        assert floor_only.route(q).covered, f"{q!r} should over-route with margin=0"
        assert not with_margin.route(q).covered, f"{q!r} should decline with the margin"


def test_confident_match_routes_without_needing_a_margin():
    # A near-exact governed-metric hit (cosine 1.0 ≥ confident) routes on its own.
    router = Router(PROFILES, HashEmbedder())
    d = router.route("quote win rate")
    assert d.covered and d.lens == "sales" and d.score >= DEFAULT_CONFIDENT


def test_eval_computes_precision_recall_and_over_decline():
    router = Router(PROFILES, HashEmbedder())
    m = evaluate(router, LABELED)
    # On this offline fixture the shipped defaults route every covered question and
    # decline every uncovered one.
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.over_declines == 0
    assert m.mis_routes == 0
    assert len(m.correct_declined) == 4  # the four uncovered questions
    assert m.total == len(LABELED)
    # the metric renders precision/recall + both error modes
    text = m.render()
    assert "precision" in text and "over-declines" in text and "mis-routes" in text


def test_over_decline_and_mis_route_are_counted_when_they_happen():
    # An impossible floor forces over-declines; verify the metric tallies them.
    strict = Router(PROFILES, HashEmbedder(), threshold=1.01)
    m = evaluate(strict, LABELED)
    assert m.over_declines == 6  # all six covered questions wrongly declined
    assert m.recall == 0.0
    # uncovered questions are still (vacuously) correctly declined
    assert len(m.correct_declined) == 4


def test_tune_threshold_picks_a_setting_that_routes_covered_and_declines_uncovered():
    tuned = tune_threshold(PROFILES, HashEmbedder(), LABELED)
    # The tuner finds a perfect setting on this fixture.
    assert tuned.metrics.precision == 1.0
    assert tuned.metrics.recall == 1.0
    assert tuned.metrics.over_declines == 0
    assert tuned.metrics.mis_routes == 0
    # and the SHIPPED defaults achieve that same perfect score (tuned, not guessed)
    shipped = evaluate(Router(PROFILES, HashEmbedder()), LABELED)
    assert shipped.correct == tuned.metrics.correct == len(LABELED)


def test_shipped_defaults_are_a_floor_margin_rule_not_a_lone_threshold():
    # The stance: the calibration is a FLOOR + MARGIN, not one absolute number.
    assert 0.0 < DEFAULT_THRESHOLD < DEFAULT_CONFIDENT <= 1.0
    assert DEFAULT_MARGIN > 0.0  # the margin is real — that's the decline lever


def test_contamination_guard_passes_when_eval_questions_are_not_anchors():
    # The uncovered + paraphrased covered questions used as labels are NOT verbatim
    # anchors here, so the guard is satisfied. (Covered labels that ARE verbatim
    # anchors are the in-fixture route test above; the guard governs the EVAL set.)
    held_out = [
        ("how many support tickets are open this week", None),
        ("what is the weather tomorrow", None),
        ("annual recurring revenue by region", "finance"),  # not a verbatim anchor
    ]
    assert_eval_held_out(held_out, PROFILES)  # does not raise


def test_contamination_guard_rejects_an_eval_question_that_is_a_verbatim_anchor():
    # If a labeled test question is verbatim in the anchors that taught the lens,
    # scoring it measures lookup, not routing — the guard refuses, reusing the
    # benchmark's exact-overlap detector.
    leaky = [("quote win rate", "sales")]  # verbatim a sales anchor
    with pytest.raises(ContaminationError):
        assert_eval_held_out(leaky, PROFILES)


def test_anchor_corpus_excludes_the_broad_description():
    # The display-only description is never part of what taught the router, so it
    # is not in the corpus the contamination guard checks against.
    corpus = " ".join(anchor_corpus(PROFILES)).lower()
    assert "quote win rate" in corpus  # a scored anchor is present
    assert "governed answers over invoices" not in corpus  # the description is not


def test_train_test_split_keeps_taught_questions_out_of_the_held_out_eval():
    # The contamination machinery the eval reuses: build anchors from TRAIN, score
    # on a disjoint TEST set — the guard then passes by construction.
    bank = [a for p in PROFILES for a in p.anchors]
    split = train_test_split(bank, test_fraction=0.4, seed=17)
    assert split.train and split.test
    assert not (set(split.train) & set(split.test))  # disjoint
    taught_profiles = [CoverageProfile(lens="finance", anchors=split.train)]
    held_out = [(q, "finance") for q in split.test]
    assert_eval_held_out(held_out, taught_profiles)  # disjoint ⇒ no leak
