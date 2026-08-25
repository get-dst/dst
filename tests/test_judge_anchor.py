"""Judge calibration machinery — agreement/kappa over decided-ticket
verdict pairs (pure functions, no DB), and the prompt-set hash every persisted
trace is stamped with."""

from __future__ import annotations

from services.reviews.anchor import _report


def test_empty_anchor_set_is_honest_none() -> None:
    r = _report([])
    assert r.n == 0 and r.agreement is None and r.kappa is None and r.confusion == {}


def test_perfect_agreement() -> None:
    r = _report([("approve", "approve"), ("reject", "reject")])
    assert r.agreement == 1.0 and r.kappa == 1.0
    assert r.confusion == {"approve:approve": 1, "reject:reject": 1}


def test_kappa_corrects_for_chance() -> None:
    # A judge that always approves against mostly-approving humans: the raw
    # rate looks good (0.8) but kappa exposes it as pure chance (0.0).
    pairs = [("approve", "approve")] * 8 + [("approve", "reject")] * 2
    r = _report(pairs)
    assert r.agreement == 0.8
    assert r.kappa == 0.0
    assert r.confusion == {"approve:approve": 8, "approve:reject": 2}


def test_degenerate_single_class_kappa_is_none() -> None:
    # Both sides always 'approve': chance agreement is 1, kappa undefined.
    r = _report([("approve", "approve")] * 3)
    assert r.agreement == 1.0 and r.kappa is None


def test_prompt_hash_is_a_short_stable_hex() -> None:
    from services.runtime.prompt_version import PROMPT_HASH

    assert len(PROMPT_HASH) == 12
    int(PROMPT_HASH, 16)  # hex or it throws
