"""The apply gate's override names the cases it covers — or it is blanket, and says so.

The blanket flag became standing policy once: an override for yesterday's red
case silently covered today's new one. A per-case override applies only when
every failing case is named; a certified divergence is never overridable.
"""

from __future__ import annotations

from services.evals.service import GateDecision
from services.project.apply import override_applies


def _blocked(*failing: str, certified: list[str] | None = None) -> GateDecision:
    return GateDecision(
        gated=True,
        blocked=True,
        failing=list(failing),
        certified_failures=certified or [],
    )


def test_per_case_override_must_name_every_failing_case() -> None:
    d = _blocked("c1", "c2")
    assert override_applies(d, "intended change", frozenset({"c1", "c2"}))
    assert override_applies(d, "intended change", frozenset({"c1", "c2", "c9"}))
    assert not override_applies(d, "intended change", frozenset({"c1"}))  # c2 is new — no
    assert not override_applies(d, "intended change", frozenset())


def test_blanket_override_still_works_but_is_blanket() -> None:
    assert override_applies(_blocked("c1"), "intended change", None)


def test_no_reason_no_override_and_certified_divergence_never_overridable() -> None:
    assert not override_applies(_blocked("c1"), None, frozenset({"c1"}))
    assert not override_applies(_blocked("c1"), "", frozenset({"c1"}))
    diverged = _blocked("c1", certified=["certified 'x' diverged"])
    assert not override_applies(diverged, "intended change", frozenset({"c1"}))
    assert not override_applies(diverged, "intended change", None)


def test_an_unblocked_decision_needs_no_override() -> None:
    open_gate = GateDecision(gated=True, blocked=False, failing=[])
    assert not override_applies(open_gate, "reason", None)
