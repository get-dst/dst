"""The certified library + run endpoints on the data plane.

run_certified serves approved SQL through guard+execute with zero generation tokens,
carrying provenance + trust_summary; the library endpoint lists/searches per lens.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.protocols import LLMResult
from services.db.session import org_session
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


def _vec(i: int) -> list[float]:
    v = [0.0] * 1024
    v[i] = 1.0
    return v


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('CertRun') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for table in ("request_log", "certified_answer", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _seed_trace(org: object, request_id: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, certification, "
                "latency, status) VALUES "
                "(:o, :r, 'customer_value', 'agent-1', 'how many repeat customers?', "
                "'SELECT count(*) FROM customers WHERE number_of_orders > 1', true, 1, "
                "'There are 19 repeat customers.', 'repeat_customer', 'high', 'none', "
                "'{}'::jsonb, 'ok')"
            ),
            {"o": org, "r": request_id},
        )


def _seed(org: object) -> str:
    with org_session(org) as session:
        if not store.lens_exists(session, "customer_value"):
            store.create_lens(session, jaffle_customer_value_bundle())
            store.publish(session, "customer_value")
        return certify_store.create(
            session,
            "customer_value",
            "how many customers are there?",
            "SELECT count(*) AS n FROM customers",
            _vec(0),
            "maija",
        )


@needs_db
def test_certified_library_lists_with_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    cert_id = _seed(org)
    try:
        r = client.get(
            "/v1/lenses/customer_value/certified", headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["lens"] == "customer_value"
        entry = next(e for e in body["certified"] if e["cert_id"] == cert_id)
        assert entry["certified_by"] == "maija"
        assert entry["certified_at"]
        assert entry["score"] is None

        # advertised on describe for agents
        d = client.get("/v1/lenses/customer_value", headers={"Authorization": f"Bearer {raw}"})
        assert d.status_code == 200
        assert d.json()["certified_count"] >= 1
        assert "how many customers are there?" in d.json()["certified_examples"]
    finally:
        _cleanup(org)


@needs_db
def test_run_certified_is_deterministic_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _make_org_token()
    cert_id = _seed(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM(["There are 100 customers."]),
    )
    try:
        r = client.post(
            f"/v1/lenses/customer_value/certified/{cert_id}/run",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["certification"] == "certified"
        assert body["certified_provenance"]["cert_id"] == cert_id
        assert body["certified_provenance"]["certified_by"] == "maija"
        assert body["trust_summary"] and "maija" in body["trust_summary"]
        assert body["data"]["rows"]

        # zero generation tokens: the single ScriptedLLM call is the composer's
        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text("SELECT ai_input_tokens, status FROM request_log WHERE request_id = :r"),
                {"r": body["request_id"]},
            ).first()
        assert row is not None and row[0] == 1 and row[1] == "ok"
    finally:
        _cleanup(org)


class _CountingLLM(ScriptedLLM):
    """Counts completions — the rows-only door must make exactly zero."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.calls = 0

    def complete(self, **kwargs: Any) -> LLMResult:
        self.calls += 1
        return super().complete(**kwargs)


@needs_db
def test_run_certified_structured_serves_rows_with_zero_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The certified door's whole latency is the prose: composing it dominates
    the request, and no programmatic caller reads it.
    ``format: "structured"`` skips the call outright — while rows, citations, provenance,
    trust_summary and the certified grade all still come back, and ``answer`` is a
    deterministic marker rather than the empty string callers read as an error."""
    org, raw = _make_org_token()
    cert_id = _seed(org)
    llm = _CountingLLM(["There are 100 customers."])
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _key, **_: llm)
    try:
        r = client.post(
            f"/v1/lenses/customer_value/certified/{cert_id}/run",
            json={"format": "structured"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert llm.calls == 0, "format=structured must not compose prose"
        assert body["data"]["rows"]
        assert [c["type"] for c in body["citations"]] == ["sql"]
        assert body["certification"] == "certified"
        assert body["certified_provenance"]["cert_id"] == cert_id
        assert body["trust_summary"] and "maija" in body["trust_summary"]
        # the skipped numeric check is not a failed one — the badge stays honest
        assert body["confidence"] == "verified"
        assert body["answer"] and "no prose was composed" in body["answer"]

        # ...and the default is untouched: prose, from one compose call.
        r2 = client.post(
            f"/v1/lenses/customer_value/certified/{cert_id}/run",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["answer"] == "There are 100 customers."
        assert r2.json()["data"]["rows"]
        assert llm.calls == 1
    finally:
        _cleanup(org)


@needs_db
def test_certify_from_request_without_embedder_stores_unembedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human APPROVE promotion must not dead-end on provider config (UI findings):
    no embedder → stored with a NULL embedding + a warning, skipped by the matcher
    until `dst reindex` backfills it."""
    org, raw = _make_org_token()
    cert_id = _seed(org)  # one embedded answer, so the matcher assertion has a hit
    _seed_trace(org, "req-no-embedder")
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    try:
        r = client.post(
            "/mgmt/lenses/customer_value/certified/from-request/req-no-embedder",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "dst reindex" in body["warning"]
        with org_session(org) as session:
            listed = certify_store.list_for_lens(session, "customer_value")
            assert {a.id for a in listed} == {cert_id, body["id"]}  # stored, with provenance
            # The matcher never sees the unembedded row — the query path cannot crash on it.
            hits = certify_store.search(session, "customer_value", _vec(0), k=5)
            assert [h.answer.id for h in hits] == [cert_id]
    finally:
        _cleanup(org)


@needs_db
def test_run_certified_unknown_id_404(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    _seed(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    try:
        r = client.post(
            "/v1/lenses/customer_value/certified/not-a-real-id/run",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 404
    finally:
        _cleanup(org)
