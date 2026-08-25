"""Query endpoint auth + lens resolution (deterministic, no LLM call)."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.deps import get_caller
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.response import QueryResponse
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle
from services.runtime.generator import GroundedSQLGenerator

client = TestClient(app)


def _seed_lens(org: object) -> None:
    with org_session(org) as session:
        if not store.lens_exists(session, "customer_value"):
            store.create_lens(session, jaffle_customer_value_bundle())
            store.publish(session, "customer_value")


def _make_org_token(admin_url: str) -> tuple[object, str]:
    admin = create_engine(admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('QTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(admin_url: str, org: object) -> None:
    admin = create_engine(admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


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


def test_query_requires_auth() -> None:
    r = client.post("/v1/lenses/customer_value/query", json={"q": "hi"})
    assert r.status_code == 401


@needs_db
def test_query_unknown_lens_404() -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('QTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    try:
        r = client.post(
            "/v1/lenses/nope/query",
            json={"q": "hi"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 404
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_query_endpoint_e2e(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full slice: endpoint -> pipeline -> sql_guard -> DuckDB -> compose -> trace logged."""
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
        '"definition_used": "repeat_customer"}'
    )
    answer = "There are repeat customers."
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # Keep the slice deterministic: no live embedding (context/certified lookups skip),
    # and pin the SQL path to the grounded generator — the scripted reply is SQL-shaped,
    # not an intent, so the metric-layer IntentSQLGenerator would reject it.
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM([gen_json, answer]),
    )
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer"] == answer
        assert body["definition_used"] == "repeat_customer"
        assert body["data"]["rows"]
        assert "customers" in body["sql"]
        assert body["confidence"] in {"verified", "partial", "unverified"}

        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text("SELECT status FROM request_log WHERE request_id = :r"),
                {"r": body["request_id"]},
            ).first()
        assert row is not None and row[0] == "ok"
    finally:
        _cleanup(settings.database_admin_url, org)


def test_query_endpoint_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler called the fully-blocking ``run_lens_query`` straight from an
    ``async def``, so ONE caller's question pinned the whole event loop for the entire
    generation + warehouse round-trip — every other caller, /health, /ready, and MCP
    (whose tools proxy back into this same loop) stalled behind it — /health went
    from sub-millisecond to the full duration of the in-flight query. Pinned two
    ways: the blocking work must leave the loop thread, and
    /health must come back while it is still running."""
    blocking_s = 1.0
    started = threading.Event()
    worker: dict[str, int] = {}

    def slow_run(
        name: str, q: str, caller: object, background: object, fmt: str = "both"
    ) -> QueryResponse:
        worker["thread"] = threading.get_ident()
        started.set()
        time.sleep(blocking_s)  # stands in for sync-httpx generation + warehouse execution
        return QueryResponse(lens=name, answer="ok", request_id="req_block")

    monkeypatch.setattr("services.api.query.run_lens_query", slow_run)
    app.dependency_overrides[get_caller] = lambda: CallerIdentity(
        org_id=uuid.uuid4(), name="t", is_admin=True
    )

    async def scenario() -> tuple[float, int, int]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            t0 = time.perf_counter()
            query = asyncio.create_task(c.post("/v1/lenses/demo/query", json={"q": "hi"}))
            # Wait for the blocking work to be under way — timing from t0, because a
            # pinned loop resumes this poll only once the block is OVER, and then the
            # /health measurement below carries the whole stall.
            while not started.is_set() and time.perf_counter() - t0 < blocking_s * 5:
                await asyncio.sleep(0.001)
            health = await c.get("/health")
            waited = time.perf_counter() - t0
            assert health.status_code == 200
            assert (await query).status_code == 200
            return waited, threading.get_ident(), health.status_code

    try:
        waited, loop_thread, _ = asyncio.run(scenario())
    finally:
        app.dependency_overrides.pop(get_caller, None)

    assert worker["thread"] != loop_thread, "run_lens_query still runs on the event-loop thread"
    assert waited < blocking_s / 2, f"/health waited {waited:.2f}s behind one in-flight query"


@needs_db
def test_lens_max_repairs_reaches_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair budget was hardcoded to the pipeline default of 1 with no way for
    a lens to raise it — a repairable engine error (say, a self-referencing
    CTE missing WITH RECURSIVE) got exactly one retry and stayed unanswered. The lens's
    ``model.max_repairs`` must reach ``run_query``; the default stays 1."""
    from services.runtime import pipeline

    org, raw = _make_org_token(settings.database_admin_url)
    bundle = jaffle_customer_value_bundle()
    assert bundle.config.model.max_repairs == 1  # untouched lenses keep today's budget
    bundle.config.model.max_repairs = 3
    with org_session(org) as session:
        store.create_lens(session, bundle)
        store.publish(session, "customer_value")

    captured: dict[str, object] = {}
    real_run_query = pipeline.run_query

    def spy(**kwargs: object) -> object:
        captured.update(kwargs)
        return real_run_query(**kwargs)  # type: ignore[arg-type]

    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
        '"definition_used": "repeat_customer"}'
    )
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM([gen_json, "There are repeat customers."]),
    )
    monkeypatch.setattr("services.api.query.run_query", spy)
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        assert captured["max_repairs"] == 3
    finally:
        admin = create_engine(settings.database_admin_url)
        with admin.begin() as c:
            for table in ("lens_version", "lens"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
        _cleanup(settings.database_admin_url, org)


def test_query_body_accepts_question_as_alias() -> None:
    # Callers reach for {"question": ...} first — accept it.
    from services.api.query import QueryBody

    assert QueryBody.model_validate({"q": "x"}).q == "x"
    assert QueryBody.model_validate({"question": "x"}).q == "x"


def test_definition_lookup_requires_auth() -> None:
    assert client.get("/v1/definitions", params={"q": "value"}).status_code == 401


@needs_db
def test_definition_lookup_is_deterministic_and_lens_scoped() -> None:
    """The read door: a governed term, found by name, without generating anything.

    Asking `route_query` "what do we mean by traded volume?" routes correctly
    but generates SQL and executes it. A definitional question must not cost a
    warehouse execution.
    """
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    try:
        headers = {"Authorization": f"Bearer {raw}"}
        r = client.get("/v1/definitions", params={"q": "repeat customer"}, headers=headers)
        assert r.status_code == 200, r.text
        # The demo lens authors it `repeat_customer`; the caller said "repeat customer".
        # Separator folding is the difference between a term hit and a body fallback.
        hits = {d["term"]: d for d in r.json()["definitions"]}
        assert "repeat_customer" in hits, hits
        assert hits["repeat_customer"]["lens"] == "customer_value"
        assert hits["repeat_customer"]["matched"] == "term"
        assert hits["repeat_customer"]["body"]

        # A term nobody governs returns empty — never a guess.
        empty = client.get("/v1/definitions", params={"q": "zzz-not-a-term"}, headers=headers)
        assert empty.status_code == 200
        assert empty.json()["definitions"] == []

        # And a blank needle is refused, not answered with the whole vocabulary.
        blank = client.get("/v1/definitions", params={"q": "  "}, headers=headers)
        assert blank.status_code == 422
    finally:
        admin = create_engine(settings.database_admin_url)
        with admin.begin() as c:
            for table in ("lens_version", "lens"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
        _cleanup(settings.database_admin_url, org)
