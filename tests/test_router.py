"""Lens router — coverage profiles + routing/decline."""

from __future__ import annotations

from services.contracts.fakes import HashEmbedder
from services.router import CoverageProfile, Router
from services.router.profiles import coverage_profile

# HashEmbedder: identical text → cosine 1.0, different text → ~0.75 (all-positive).
# Tests route on EXACT anchor matches with a high threshold; semantic quality is a
# live/Voyage concern, not this unit's job.
HI = 0.95


def _profiles() -> list[CoverageProfile]:
    return [
        CoverageProfile(
            lens="finance",
            anchors=["What is our revenue?", "outstanding receivables", "net invoiced"],
        ),
        CoverageProfile(
            lens="sales",
            anchors=["Which rep won the most deals?", "quote win rate", "pipeline"],
        ),
    ]


def test_routes_a_question_to_the_lens_whose_anchor_it_matches():
    router = Router(_profiles(), HashEmbedder(), threshold=HI)
    d = router.route("What is our revenue?")
    assert d.covered and d.lens == "finance" and d.score >= HI
    d2 = router.route("Which rep won the most deals?")
    assert d2.covered and d2.lens == "sales"


def test_declines_when_no_lens_covers_the_question():
    router = Router(_profiles(), HashEmbedder(), threshold=HI)
    d = router.route("How many support tickets are open this week?")
    assert not d.covered and d.lens is None
    assert "no governed lens covers this" in d.reason
    # the decline still ranks the nearest miss for diagnostics
    assert d.alternatives and d.alternatives[0][1] < HI


def test_alternatives_are_ranked_best_first():
    router = Router(_profiles(), HashEmbedder(), threshold=HI)
    d = router.route("What is our revenue?")
    assert d.lens == "finance"
    # the runner-up (sales) appears in alternatives, scored below the winner
    assert d.alternatives[0][0] == "sales" and d.alternatives[0][1] <= d.score


def test_no_lenses_is_a_clean_decline_not_a_crash():
    d = Router([], HashEmbedder()).route("anything")
    assert not d.covered and d.reason == "no lenses to route to"


def test_coverage_profile_is_auto_derived_from_the_lens():
    from services.lenses.demo import jaffle_customer_value_bundle

    prof = coverage_profile(jaffle_customer_value_bundle())
    assert prof.lens == "customer_value"
    text = " ".join(prof.anchors).lower()
    assert "repeat customer" in text  # a definition term
    assert "how many orders were placed in total?" in text  # a sample question
    assert "customers" in text and "orders" in text  # entities
    assert "customers" in prof.scope  # allowed table, for the route sanity-check


def test_broad_description_is_recorded_for_display_but_not_scored():
    # The broad "<lens>: <summary>" blurb matches almost anything, so it
    # is kept for display only (`description`) and OUT of the scored `anchors`.
    from services.lenses.demo import jaffle_customer_value_bundle

    prof = coverage_profile(jaffle_customer_value_bundle())
    assert "customer value" in prof.description.lower()  # kept for display
    anchors = " ".join(prof.anchors).lower()
    # the broad description phrase is NOT in the scoring set
    assert "order activity over the jaffle dataset" not in anchors
