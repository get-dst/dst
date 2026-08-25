"""Contamination guard — train/test split, overlap detector, metric separation."""

from __future__ import annotations

import pytest

from services.benchmark.contamination import (
    ContaminationError,
    assert_held_out,
    find_leaked,
    is_taught,
    normalize,
    split_metrics,
    taught_set,
    train_test_split,
)

CERTIFIED = ["What is our total net invoiced amount in euros?", "How many customers do we have?"]


def test_normalize_collapses_case_and_punctuation():
    assert normalize("What's our  Revenue?!") == "what s our revenue"


def test_taught_set_and_is_taught_match_on_normalized_text():
    taught = taught_set(CERTIFIED)
    assert is_taught("WHAT IS OUR total net invoiced amount in euros", taught)  # case/punct variant
    assert not is_taught("What was revenue in March 2026?", taught)  # a held-out cut


def test_overlap_detector_refuses_to_score_taught_questions():
    taught = taught_set(CERTIFIED)
    leaked = find_leaked(["What was revenue in March?", "how many customers do we have"], taught)
    assert leaked == ["how many customers do we have"]
    with pytest.raises(ContaminationError, match="verbatim in the certified"):
        assert_held_out(["how many customers do we have"], taught)
    # a clean held-out set passes silently
    assert_held_out(["What was revenue in March 2026?"], taught)


def test_train_test_split_is_disjoint_and_deterministic():
    bank = [f"q{i}" for i in range(10)]
    a = train_test_split(bank, test_fraction=0.4, seed=3)
    b = train_test_split(bank, test_fraction=0.4, seed=3)
    assert a == b  # deterministic
    assert set(a.train).isdisjoint(a.test)
    assert len(a.test) == 4 and len(a.train) == 6
    assert set(a.train) | set(a.test) == set(bank)  # nothing lost


def test_metric_separation_taught_never_counts_as_reasoning():
    taught = taught_set(CERTIFIED)
    graded = [
        ("How many customers do we have?", True),  # taught → coverage, not reasoning
        # taught → coverage even when wrong:
        ("What is our total net invoiced amount in euros?", False),
        ("What was revenue in March 2026?", True),  # held-out, correct
        ("What was revenue in 2025?", False),  # held-out, wrong
        ("Revenue for Bathroom renovation?", True),  # held-out, correct
    ]
    m = split_metrics(graded, taught)
    assert len(m.taught) == 2
    assert (len(m.held_out_correct), len(m.held_out_wrong)) == (2, 1)
    assert m.coverage == 2 / 5  # 2 of 5 questions are pre-answered
    assert m.reasoning_accuracy == 2 / 3  # held-out only
    # there is deliberately NO blended-accuracy property
    assert not hasattr(m, "accuracy")
    assert "coverage" in m.render() and "reasoning accuracy" in m.render()


def test_empty_held_out_yields_none_not_a_crash():
    taught = taught_set(CERTIFIED)
    m = split_metrics([("How many customers do we have?", True)], taught)
    assert m.reasoning_accuracy is None and m.coverage == 1.0
