"""The pure regression-gate decision (warn vs block). No DB/connector/LLM."""

from __future__ import annotations

from services.evals.service import gate_decision


def test_block_refuses_on_regression() -> None:
    d = gate_decision(gate="block", score=0.80, prev_score=0.95, failing=["c1"])
    assert d.gated and d.regressed and d.blocked and d.failing == ["c1"]


def test_block_allows_when_score_holds() -> None:
    d = gate_decision(gate="block", score=0.95, prev_score=0.95, failing=[])
    assert d.gated and not d.regressed and not d.blocked


def test_block_allows_on_improvement() -> None:
    d = gate_decision(gate="block", score=1.0, prev_score=0.9, failing=[])
    assert not d.regressed and not d.blocked


def test_warn_surfaces_regression_but_never_blocks() -> None:
    d = gate_decision(gate="warn", score=0.5, prev_score=0.9, failing=["c1", "c2"])
    assert d.gated and d.regressed and not d.blocked


def test_first_run_has_no_baseline_to_regress_against() -> None:
    d = gate_decision(gate="block", score=0.7, prev_score=None, failing=[])
    assert not d.regressed and not d.blocked
