"""'Use this when…' routing examples: generation + router-anchor wiring."""

from __future__ import annotations

from services.contracts.fakes import ScriptedLLM
from services.lenses.demo import jaffle_customer_value, jaffle_customer_value_bundle
from services.lenses.use_when import generate_use_when
from services.router.profiles import coverage_profile


def test_generate_use_when_parses_and_dedupes() -> None:
    llm = ScriptedLLM(
        [
            '{"use_when": ["how many repeat customers?", "lifetime value by cohort", '
            '"how many repeat customers?"]}'
        ]
    )
    out = generate_use_when(
        llm, "m", jaffle_customer_value(), display_name="Customer Value", description="customers"
    )
    assert out == ["how many repeat customers?", "lifetime value by cohort"]  # deduped, ordered


def test_generate_use_when_is_empty_on_bad_output() -> None:
    llm = ScriptedLLM(["not json at all"])
    out = generate_use_when(llm, "m", jaffle_customer_value(), display_name="x", description="y")
    assert out == []


def test_use_when_examples_become_router_anchors() -> None:
    # The whole point: a curated 'use this when…' phrasing is a scored routing anchor, so
    # a question asked that way matches near-exactly — what plain metric anchors miss.
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.use_when = ["how are we doing on repeat business?"]
    prof = coverage_profile(bundle)
    assert "how are we doing on repeat business?" in prof.anchors
