"""OpenAI-compatible adapter (the app door): /v1/chat/completions + /v1/models.

The adapter's job is purely the wire-format mapping around the shared governed core
``run_lens_query`` (parity with REST/MCP is structural — same function). So these tests
isolate the adapter: real auth for the 401 paths, and a stubbed core + caller override
for the mapping/streaming/error paths. The full pipeline is covered by test_query_api.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.app import app
from services.auth.deps import get_caller
from services.contracts.response import Citation, DataPayload, QueryResponse
from services.governance.credentials import CallerIdentity

client = TestClient(app)

_ANSWER = "There are 42 repeat customers."


def _resp() -> QueryResponse:
    return QueryResponse(
        lens="customer_value",
        answer=_ANSWER,
        data=DataPayload(columns=["n"], rows=[[42]]),
        sql="SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",
        definition_used="repeat_customer",
        citations=[Citation(type="definition", ref="repeat_customer")],
        confidence="verified",
        certification="none",
        request_id="req_testabc123",
    )


@pytest.fixture
def as_caller() -> Iterator[CallerIdentity]:
    """Override the auth dependency so mapping tests don't need a DB-backed token."""
    fake = CallerIdentity(org_id=uuid.uuid4(), name="t", is_admin=True)
    app.dependency_overrides[get_caller] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_caller, None)


# --------------------------------------------------------------------------- #
# Auth — exercises the real get_caller dependency (no override).
# --------------------------------------------------------------------------- #
def test_chat_completions_requires_auth() -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "dst/customer_value", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_models_requires_auth() -> None:
    assert client.get("/v1/models").status_code == 401


# --------------------------------------------------------------------------- #
# Mapping — stub the shared core, assert the OpenAI wire format.
# --------------------------------------------------------------------------- #
def test_chat_completions_maps_response(
    as_caller: CallerIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(name: str, q: str, caller: object, background: object) -> QueryResponse:
        captured["lens"], captured["q"] = name, q
        return _resp()

    monkeypatch.setattr("services.api.openai_compat.run_lens_query", fake_run)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "dst/customer_value",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "how many repeat customers?"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    # model → lens, last user message → question
    assert captured == {"lens": "customer_value", "q": "how many repeat customers?"}
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "dst/customer_value"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": _ANSWER}
    assert body["choices"][0]["finish_reason"] == "stop"
    extras = body["dst"]
    assert extras["lens"] == "customer_value"
    assert "customers" in extras["sql"]
    assert extras["definition_used"] == "repeat_customer"
    assert extras["data"]["rows"] == [[42]]
    # id mirrors request_id so Observe / send_for_review still correlate.
    assert body["id"] == extras["request_id"] == "req_testabc123"


def test_chat_completions_accepts_bare_lens_and_content_parts(
    as_caller: CallerIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(name: str, q: str, caller: object, background: object) -> QueryResponse:
        captured["lens"], captured["q"] = name, q
        return _resp()

    monkeypatch.setattr("services.api.openai_compat.run_lens_query", fake_run)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "customer_value",  # bare, no dst/ prefix
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi there"}]}],
        },
    )
    assert r.status_code == 200, r.text
    assert captured == {"lens": "customer_value", "q": "hi there"}


def test_chat_completions_streaming(
    as_caller: CallerIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.api.openai_compat.run_lens_query",
        lambda name, q, caller, background: _resp(),
    )
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "dst/customer_value",
            "messages": [{"role": "user", "content": "how many repeat customers?"}],
            "stream": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "chat.completion.chunk" in body
    assert _ANSWER in body
    assert '"dst"' in body
    assert body.rstrip().endswith("data: [DONE]")


# --------------------------------------------------------------------------- #
# Errors — HTTPException from the core → OpenAI error envelope.
# --------------------------------------------------------------------------- #
def test_chat_completions_core_error_becomes_envelope(
    as_caller: CallerIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(name: str, q: str, caller: object, background: object) -> QueryResponse:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found or not published")

    monkeypatch.setattr("services.api.openai_compat.run_lens_query", boom)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "dst/nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404
    assert "not found" in r.json()["error"]["message"]


def test_chat_completions_missing_lens_in_model_400(as_caller: CallerIdentity) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "dst", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_no_user_message_400(as_caller: CallerIdentity) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "dst/customer_value", "messages": [{"role": "system", "content": "x"}]},
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Models — permitted lenses as OpenAI model objects.
# --------------------------------------------------------------------------- #
def test_models_lists_permitted_lenses(
    as_caller: CallerIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Bundle:
        config = object()

    monkeypatch.setattr(
        "services.api.openai_compat.list_published_for_org",
        lambda org_id: [("customer_value", "Customer Value", "desc", _Bundle())],
    )
    monkeypatch.setattr("services.api.openai_compat.authorize", lambda caller, config: (True, ""))
    r = client.get("/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "dst/customer_value"
    assert body["data"][0]["owned_by"] == "dst"
