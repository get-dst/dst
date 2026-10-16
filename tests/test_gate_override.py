"""The apply gate's override names the cases it covers — or it is blanket, and says so.

The blanket flag became standing policy once: an override for yesterday's red
case silently covered today's new one. A per-case override applies only when
every failing case is named, a certified divergence among them.
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


def test_no_reason_no_override() -> None:
    assert not override_applies(_blocked("c1"), None, frozenset({"c1"}))
    assert not override_applies(_blocked("c1"), "", frozenset({"c1"}))


def test_an_unblocked_decision_needs_no_override() -> None:
    open_gate = GateDecision(gated=True, blocked=False, failing=[])
    assert not override_applies(open_gate, "reason", None)


def test_a_named_override_covers_a_certified_divergence_it_names() -> None:
    """A certified divergence has an id like every failing case
    (``certified:<answer id>``): an override that names it covers it, one
    that does not leaves the block in place."""
    diverged = _blocked("certified:ca_1", "c1", certified=["certified 'x' diverged"])
    assert override_applies(diverged, "intended change", frozenset({"certified:ca_1", "c1"}))
    assert not override_applies(diverged, "intended change", frozenset({"c1"}))
    assert override_applies(diverged, "intended change", None)  # blanket


def test_the_cli_flag_takes_the_case_ids_it_covers(monkeypatch) -> None:
    """``--allow-failing-cases certified:ca_1 c2`` names its cases and rides
    as per-case ids; bare, it is the blanket form; both need --reason."""
    import argparse

    import httpx

    from services.cli import main as cli

    class _Sent(Exception):
        def __init__(self, params: object) -> None:
            self.params = params

    def _capture(url, headers=None, params=None, json=None, timeout=None):  # noqa: ANN001
        raise _Sent(params)

    monkeypatch.setattr(cli, "_project_files", lambda args, verb: {})
    monkeypatch.setattr(cli, "_client", lambda args: ("http://x", {}))
    monkeypatch.setattr(httpx, "post", _capture)

    def _params(**over: object) -> dict[str, object]:
        ns = argparse.Namespace(
            **{
                "allow_failing_cases": None,
                "allow_case": None,
                "reason": None,
                "timeout": 1.0,
                **over,
            }
        )
        try:
            cli._apply(ns)
        except _Sent as sent:
            return dict(sent.params or {})  # type: ignore[call-overload]  # None = no params
        raise AssertionError("apply never posted")

    named = _params(allow_failing_cases=["certified:ca_1", "c2"], reason="meant")
    assert named["allow_case"] == ["certified:ca_1", "c2"]
    assert "allow_failing_cases" not in named and named["override_reason"] == "meant"
    blanket = _params(allow_failing_cases=[], reason="meant")
    assert blanket["allow_failing_cases"] == "true" and "allow_case" not in blanket
    assert "override_reason" not in _params()
    bare = argparse.Namespace(allow_failing_cases=[], allow_case=None, reason="", timeout=1.0)
    assert cli._apply(bare) == 2
