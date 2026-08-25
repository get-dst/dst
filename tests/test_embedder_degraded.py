"""A dead embedder must be LOUD.

The failure mode: fastembed defaults its model cache into
``tempfile.gettempdir()``, the OS reaps it, and the ONNX session fails
NO_SUCHFILE — from there ``embed_question`` returns None, ``certified_lookup``
short-circuits to "none", and certified matching cannot fire for ANY question.
Nothing says so: the responses look like ordinary generated answers, the trace
looks ordinary, and /ready reports the embedder as "unknown".

Three things are pinned here: the model cache lives somewhere durable, a
degraded serve says it is degraded on the response AND in the persisted trace,
and /ready names the cause.
"""

from __future__ import annotations

import json
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.config import settings
from services.context import local_embedder
from services.contracts.fakes import HashEmbedder, ScriptedLLM, fake_llm_providers
from services.runtime import assembly
from services.runtime.generator import GroundedSQLGenerator
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)

_GEN_JSON = (
    '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
    '"definition_used": null}'
)


def _dead_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly the live shape: resolution RAISES because the weights are gone."""

    def _boom() -> None:
        raise RuntimeError("NO_SUCHFILE: model_optimized.onnx failed. File doesn't exist")

    monkeypatch.setattr("services.llm.registry.resolve_embedder", _boom)


def _scripted_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM([_GEN_JSON, "There are 19."]),
    )


# ── the model cache lives somewhere the OS does not reap ──────────────────────


def test_the_model_cache_is_not_in_a_directory_the_os_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    default = local_embedder.cache_dir()
    assert not str(default).startswith(tempfile.gettempdir()), (
        f"the model cache is back under the OS's scratch directory: {default}"
    )
    assert default.parts[-2:] == ("dst", "fastembed")
    # fastembed's own knob still wins — passing cache_dir= explicitly would
    # otherwise stop fastembed from reading it, silently ignoring an operator.
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/models/fastembed")
    assert str(local_embedder.cache_dir()) == "/models/fastembed"


# ── the response and the trace say the matching layer is down ─────────────────


@needs_db
def test_a_dead_embedder_is_loud_on_the_response_and_in_the_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    _scripted_generation(monkeypatch)
    _dead_embedder(monkeypatch)
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The answer still serves — degradation is fail-open, as it always was.
        assert body["certification"] == "none"
        # …but "none" no longer means "the corpus has nothing for you".
        assert body["degraded"], "a serve with certified matching DOWN said nothing"
        note = body["degraded"][0]
        assert note.startswith("DEGRADED: certified matching did not run")
        assert "NO_SUCHFILE" in note, "the note must name the cause, not just the symptom"

        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            stored = c.execute(
                text("SELECT degraded FROM request_log WHERE request_id = :r"),
                {"r": body["request_id"]},
            ).scalar_one()
        # jsonb: psycopg may hand it back parsed or raw depending on the driver.
        stored = json.loads(stored) if isinstance(stored, str) else stored
        assert stored == body["degraded"], "the trace must carry what the response carried"
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_a_healthy_serve_carries_no_degraded_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of loud: a note on every response would be noise nobody
    reads. It appears only when a capability actually could not run."""
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    _scripted_generation(monkeypatch)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", HashEmbedder)
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["degraded"] == []
    finally:
        _cleanup(settings.database_admin_url, org)


def test_assemble_reports_an_unconfigured_embedder_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """No embedder AT ALL is the same outage with a different cause — an OSS
    install that never configured one cannot match either, and the caller is
    owed the same sentence."""
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    vec, why = assembly.embed_question_degraded("how many repeat customers?")
    assert vec is None and why == assembly.NO_EMBEDDER


# ── /ready names the cause instead of shrugging ───────────────────────────────


def test_ready_names_a_dead_embedder_instead_of_reporting_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_embedder` raising used to land in the blanket handler and report
    "unknown" — the operator's one health string said nothing while matching was
    down for the whole process."""
    _dead_embedder(monkeypatch)
    body = client.get("/ready").json()
    assert body["embeddings"].startswith("unavailable ("), body["embeddings"]
    assert "NO_SUCHFILE" in body["embeddings"]
    assert body["certified_matching"] == "unavailable"


def test_ready_reports_matching_unavailable_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    body = client.get("/ready").json()
    assert body["embeddings"] == "unconfigured"
    assert body["certified_matching"] == "unavailable"


def test_ready_reports_matching_ok_when_the_embedder_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.llm.registry.resolve_embedder", HashEmbedder)
    assert client.get("/ready").json()["certified_matching"] == "ok"
