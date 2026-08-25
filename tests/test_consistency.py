"""Ground-truth-free consistency — assessment + the 2×2 validation math."""

from __future__ import annotations

from services.probe.consistency import ConsistencyReport, ConsistencyRow, is_consistent


def test_is_consistent_handles_agreement_disagreement_and_unknown():
    assert is_consistent([100.0, 100.0, 100.05]) is True  # within tol
    assert is_consistent([100.0, 130.0]) is False  # diverge
    assert is_consistent([100.0, None]) is None  # only one assessable
    assert is_consistent([None, None]) is None
    assert is_consistent([5.0, 5.0]) is True  # integer counts, exact


def _row(consistent, correct):
    nums = [1.0, 1.0] if consistent else [1.0, 2.0]
    return ConsistencyRow("q", nums, consistent, correct)


def test_two_by_two_rates_and_blind_spot():
    # A signal that predicts well: contradictory→mostly wrong, consistent→mostly correct
    rep = ConsistencyReport(
        rows=[
            _row(False, False),  # caught
            _row(False, False),  # caught
            _row(False, True),  # false alarm
            _row(True, True),  # clean
            _row(True, True),  # clean
            _row(True, False),  # blind spot
        ]
    )
    assert rep.contradictory_wrong == 2 and rep.contradictory_correct == 1
    assert rep.consistent_correct == 2 and rep.consistent_wrong == 1
    assert abs(rep.p_wrong_given_contradictory - 2 / 3) < 1e-9
    assert abs(rep.p_wrong_given_consistent - 1 / 3) < 1e-9
    assert abs(rep.catch_rate - 2 / 3) < 1e-9  # 2 of 3 real errors flagged
    assert rep.phi is not None and rep.phi > 0  # positive association


def test_unassessable_rows_excluded_from_stats():
    rep = ConsistencyReport(rows=[ConsistencyRow("q", [1.0], None, False), _row(True, True)])
    assert len(rep.assessable) == 1
    assert "n=1 assessable of 2" in rep.render()


def test_perfect_predictor_has_phi_one():
    rep = ConsistencyReport(rows=[_row(False, False), _row(True, True)])
    assert rep.phi == 1.0
