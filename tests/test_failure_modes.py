"""The failure-mode vocabulary: twenty names, one per non-answer.

Three things are pinned here. The vocabulary itself and the rename from the
older names (``ClarificationRequest.kind`` and its slots, the guard's verdicts,
drift kinds, plan codes). The static half of the tripwire: every place in
``services/`` that builds a refusal, a rejection or a clarification names its
mode, read off the source so a site no test reaches is still covered (the
session half lives in conftest.py and watches what the suite builds). And the
receipt: a mode is signed once set and leaves the canonical bytes alone while
unset, so receipts issued before it still verify.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.config import ProviderConfig, settings
from services.contracts.failure_modes import (
    DRIFT_MODES,
    HINT_MODES,
    FailureMode,
    clarification_mode,
)
from services.contracts.response import ClarificationRequest, QueryResponse
from services.lenses.profile_drift import ProfileDrift
from services.runtime import receipt, sql_guard
from services.validate.report import Issue, validate_bundle
from tests.test_byok_model_default import _bundle

SERVICES = Path(__file__).resolve().parents[1] / "services"
_NON_ANSWERS = {"refused", "rejected", "clarification"}


def test_the_vocabulary_is_twenty_names() -> None:
    assert len(FailureMode) == 20
    assert {str(m) for m in FailureMode} == {m.name for m in FailureMode}


@pytest.mark.parametrize(
    ("kind", "term", "options", "mode"),
    [
        ("ambiguous_term", "revenue", ["a", "b"], FailureMode.ambiguous_metric),
        ("unknown_value", "status", ["open"], FailureMode.unbound_value),
        ("named_table", "orders", [], FailureMode.scope_violation),
        ("unresolved_slot", "metric", ["a", "b"], FailureMode.ambiguous_metric),
        ("unresolved_slot", "metric", [], FailureMode.uncovered_word),
        ("unresolved_slot", "window", [], FailureMode.period_unreadable),
        ("unresolved_slot", "compare", [], FailureMode.period_unreadable),
        ("unresolved_slot", "dimension", [], FailureMode.wrong_grain),
        ("unresolved_slot", "grain", [], FailureMode.wrong_grain),
        ("unresolved_slot", "entity", [], FailureMode.not_declared),
        ("unresolved_slot", "filter_value", [], FailureMode.unbound_value),
        ("unresolved_slot", "shape", [], FailureMode.uncovered_word),
        ("unresolved_slot", "customer_type", [], FailureMode.unbound_value),
    ],
)
def test_the_older_names_map_one_to_one(
    kind: str, term: str, options: list[str], mode: FailureMode
) -> None:
    assert clarification_mode(kind, term, options) is mode
    clar = ClarificationRequest(kind=kind, term=term, question="?", options=options)  # type: ignore[arg-type]
    assert clar.mode is mode and clar.kind == kind  # the old name stays populated


def test_a_construction_site_names_the_mode_over_the_derived_one() -> None:
    clar = ClarificationRequest(
        kind="unresolved_slot", term="metric", question="?", mode=FailureMode.not_expressible
    )
    assert clar.mode is FailureMode.not_expressible


def test_a_clarification_response_carries_its_mode() -> None:
    clar = ClarificationRequest(kind="unknown_value", term="status", question="?")
    r = QueryResponse(
        lens="l", status="clarification", answer="?", clarification=clar, request_id="r"
    )
    assert r.mode is FailureMode.unbound_value
    # A row stored before the field existed reads back with the mode derived.
    old = QueryResponse.model_validate(
        {
            "lens": "l",
            "status": "clarification",
            "answer": "?",
            "request_id": "r",
            "clarification": {"term": "window", "question": "?", "kind": "unresolved_slot"},
        }
    )
    assert old.mode is FailureMode.period_unreadable


def test_guard_verdicts_carry_the_mode_and_a_syntax_fault_carries_none() -> None:
    assert sql_guard.GuardResult(False, reason="table 'x' is out of lens scope").mode is (
        FailureMode.scope_violation
    )
    assert sql_guard.GuardResult(False, reason="parse error", kind="syntax").mode is None
    assert sql_guard.GuardResult(True, sql="SELECT 1").mode is None


def test_drift_kinds_that_change_a_reader_are_stale_and_the_quiet_ones_carry_none() -> None:
    assert ProfileDrift(table="t", kind="column_dropped", detail="c").mode is FailureMode.stale
    for quiet in ("column_added", "table_added", "column_recommented", "column_shifted"):
        assert ProfileDrift(table="t", kind=quiet, detail="c").mode is None  # type: ignore[arg-type]
    assert set(DRIFT_MODES.values()) == {FailureMode.stale}


def test_a_plan_code_names_the_mode_it_prevents() -> None:
    assert Issue(severity="warning", code="value_aliases_unbound", message="m").mode is (
        FailureMode.unbound_value
    )
    assert Issue(severity="error", code="no_such_code", message="m").mode is None
    assert HINT_MODES["lens_model_unservable"] is FailureMode.provider_down


def _receipt(mp: pytest.MonkeyPatch, **extra: object) -> receipt.Receipt:
    mp.setattr(settings, "secret_key", "k1")
    return receipt.build(
        request_id="req_r1",
        lens="l",
        certification="none",
        cert_id=None,
        confidence="partial",
        sql="SELECT 1",
        data_as_of="2026-08-11",
        **extra,  # type: ignore[arg-type]
    )


def test_an_unset_mode_leaves_the_receipt_bytes_alone_and_a_set_one_is_signed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _receipt(monkeypatch)
    assert b'"mode"' not in receipt._canonical(plain)
    capped = _receipt(monkeypatch, mode=FailureMode.data_gap)
    assert receipt.check_signature(capped) == "valid"
    forged = capped.model_copy(update={"mode": FailureMode.stale})
    assert receipt.check_signature(forged) == "invalid"


# ── the apply gate demands only the providers the lens's lanes use ───────────


def test_a_typed_only_lens_lands_on_a_typed_decision_provider_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "providers", {"jev": ProviderConfig(type="typesafe", api_key="k")}
    )
    report = validate_bundle(_bundle(), [], [])
    assert report.ok, [i.message for i in report.issues if i.severity == "error"]
    issue = next(i for i in report.issues if i.code == "lens_model_unservable")
    assert issue.severity == "warning" and "503" in issue.message


def test_a_lens_that_falls_to_raw_sql_still_needs_its_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "providers", {"jev": ProviderConfig(type="typesafe", api_key="k")}
    )
    bundle = _bundle()
    bundle.config.untyped_fallback = True
    assert not validate_bundle(bundle, [], []).ok


# ── the static tripwire: every non-answer built in services/ names its mode ──


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {k.arg: k.value for k in call.keywords if k.arg}


def _name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _modeless_sites() -> list[str]:
    out: list[str] = []
    for path in sorted(SERVICES.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kw = _keywords(node)
            where = f"{path.relative_to(SERVICES.parent)}:{node.lineno}"
            name = _name(node)
            if name == "QueryResponse":
                status = kw.get("status")
                if (
                    isinstance(status, ast.Constant)
                    and status.value in _NON_ANSWERS
                    and "mode" not in kw
                    and "clarification" not in kw
                ):
                    out.append(where)
            elif name == "_trace_failure" and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and first.value in ("refused", "rejected")
                    and "mode" not in kw
                ):
                    out.append(where)
            elif name == "GuardResult":
                # A policy verdict defaults to scope_violation; the hide and the
                # external reach must say which they are.
                reason = ast.unparse(kw.get("reason", ast.Constant(""))).lower()
                if ("hidden" in reason or "external" in reason) and "mode" not in kw:
                    out.append(where)
    return out


def test_every_refusal_and_clarification_site_names_a_mode() -> None:
    sites = _modeless_sites()
    assert not sites, "non-answers built without a failure mode:\n" + "\n".join(sites)


def test_the_static_scan_sees_the_sites_it_checks() -> None:
    """Against a vacuous pass: the scan must find the pipeline's refusals at
    all, or a rename of the helper would silently empty it."""
    text = (SERVICES / "runtime" / "pipeline.py").read_text()
    tree = ast.parse(text)
    seen = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _name(n) == "_trace_failure" and "mode" in _keywords(n)
    ]
    assert len(seen) >= 10


def test_the_session_tripwire_is_armed() -> None:
    from services.api.route import UncoveredEnvelope
    from tests import conftest

    assert QueryResponse.__init__.__name__ == "init"
    assert UncoveredEnvelope.__init__.__name__ == "init"
    # A test's own construction is a fake: built here, it is not counted.
    before = conftest.MODE_CHECKED[0]
    QueryResponse(lens="l", status="refused", answer="no", request_id="r")
    assert conftest.MODE_CHECKED[0] == before
