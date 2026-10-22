"""The publish gate and `dst test` grade the response the query door serves.

The behavioural lane used to run its cases on a harness of its own, which
always accepted an untyped answer. On a typed-only lens (`untyped_fallback:
false`, the default) the door clarifies or declines a question that does not
type; the harness instead escalated to raw-SQL generation and answered or
refused. Cases pinned to what the door does failed the gate, so every apply
needed an override, and the gate certified a path no caller is served.

Pinned here: the lane's serve core is the door's own, so the same question on
the same lens comes back in the same shape on both, with the lens's fallback
switch either way; a clarify case passes when the door clarifies; and a
clarify that fails says what came back instead of calling a refusal an answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.app import app
from services.config import settings
from services.contracts.eval import EvalCase
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.protocols import Decision
from services.db.session import org_session
from services.evals.runner import run_behavioral
from services.evals.service import lens_serve
from services.lenses import store
from services.lenses.connections import resolve_connector
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm import registry
from services.runtime import assembly
from tests.test_embedder_degraded import _dead_embedder
from tests.test_query_api import _cleanup, _make_org_token, needs_db

client = TestClient(app)

QUESTION = "How many customers placed more than one order?"
_RAW_SQL = (
    '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
    '"definition_used": null}'
)


class _Declines:
    """A typed decider that picks nothing: the question never types."""

    def decide(self, *, question: str, context: str, options: Any, allow_none: bool) -> Decision:
        return Decision(chosen=None, probs={"none": 1.0}, provider="t")


def _publish(org: object, *, untyped_fallback: bool) -> store.LensBundle:
    bundle = jaffle_customer_value_bundle()
    bundle.config = bundle.config.model_copy(update={"untyped_fallback": untyped_fallback})
    with org_session(org) as session:
        if store.lens_exists(session, bundle.config.name):
            store.update_draft(session, bundle.config.name, bundle)
        else:
            store.create_lens(session, bundle)
        store.publish(session, bundle.config.name)
    return bundle


def _typed_serving_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(settings, "typed_serving", "on")
    monkeypatch.setattr(assembly, "typed_decider", lambda: _Declines())
    # Raw-SQL generation, if anything reaches it, answers the question.
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM([_RAW_SQL, "There are 19."]),
    )
    _dead_embedder(monkeypatch)


def _door(raw: str) -> str:
    r = client.post(
        "/v1/lenses/customer_value/query",
        json={"q": QUESTION},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["status"])


def _gate(org: object, bundle: store.LensBundle) -> tuple[str, Any]:
    resolved = registry.resolve(bundle.config.model.model_ref())
    assert resolved is not None
    serve = lens_serve(
        bundle.config.name,
        bundle,
        org_id=str(org),
        connector=resolve_connector(bundle.config.connections[0], org),
        resolved=resolved,
    )
    return serve(QUESTION).response.status, serve


@needs_db
@pytest.mark.parametrize("untyped_fallback", [False, True])
def test_a_case_comes_back_in_the_shape_the_door_serves(
    monkeypatch: pytest.MonkeyPatch, untyped_fallback: bool
) -> None:
    _typed_serving_declines(monkeypatch)
    org, raw = _make_org_token(settings.database_admin_url)
    try:
        bundle = _publish(org, untyped_fallback=untyped_fallback)
        door = _door(raw)
        gate, _ = _gate(org, bundle)
        assert gate == door
        if untyped_fallback:
            assert door == "ok"  # escalated to raw SQL, on both
        else:
            assert door != "ok"  # typed-only: never the raw-SQL answer
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_a_pin_to_the_door_s_shape_passes_the_behavioural_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _typed_serving_declines(monkeypatch)
    org, raw = _make_org_token(settings.database_admin_url)
    try:
        bundle = _publish(org, untyped_fallback=False)
        door = _door(raw)
        expect = {"clarification": "clarify", "refused": "refuse"}[door]
        _, serve = _gate(org, bundle)
        case = EvalCase(
            id="c1",
            lens="customer_value",
            question=QUESTION,
            expect=expect,  # type: ignore[arg-type]
            source="authored",
            status="approved",
        )
        outcome = run_behavioral(lens="customer_value", cases=[case], serve=serve, concurrency=1)
        (result,) = outcome.results
        assert result.passed, result.reason
        # The opposite pin fails, and its reason names what came back.
        other = case.model_copy(update={"expect": "answer"})
        (failed,) = run_behavioral(
            lens="customer_value", cases=[other], serve=serve, concurrency=1
        ).results
        assert not failed.passed and f"status={door}" in (failed.reason or "")
    finally:
        _cleanup(settings.database_admin_url, org)
