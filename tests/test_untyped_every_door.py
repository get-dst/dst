"""A raw-SQL answer says so on every door, in the same words.

An answer that did not come from the typed reading carries an ``UNTYPED:`` line in
``degraded`` and ``typed: false`` in ``resolution``. It went missing two ways: the
query door replaced the pipeline's ``degraded`` lines with its own line whenever
certified matching could not run, and a typed reading that failed a check before
serving was repaired by raw-SQL generation without writing the line at all, so the
JSON said ``typed: false`` while ``dst query`` printed nothing about it.

Pinned here: the pipeline writes the line on the repair path, and the API JSON, the
MCP tool, ``dst query`` and the OpenAI-compatible door carry the same lines and the
same resolution tag.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.api import query as query_api
from services.app import app
from services.config import settings
from services.contracts.fakes import EchoQueryGenerator, ScriptedLLM, fake_llm_providers
from services.contracts.protocols import GeneratedQuery
from services.lenses.demo import jaffle_customer_value
from services.mcp import server as mcp_server
from services.runtime.pipeline import run_query
from tests.test_embedder_degraded import _dead_embedder
from tests.test_pipeline import _conn
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)

COUNT = "SELECT count(*) AS n FROM customers"
QUESTION = "How many customers are there?"


class _TypedFailsACheck:
    """A typed reading whose SQL does not survive the checks before serving."""

    model = "typed"

    def generate(self, **_kw: object) -> GeneratedQuery:
        return GeneratedQuery(sql="SELECT no_such_column FROM customers", typed=True)


class _DoesNotType:
    """A typed reading that cannot bind the question."""

    model = "typed"

    def generate(self, **_kw: object) -> GeneratedQuery:
        return GeneratedQuery(sql="", no_answer_reason="no declared metric counts customers")


def test_a_typed_reading_repaired_by_raw_sql_is_disclosed_untyped() -> None:
    res = run_query(
        question=QUESTION,
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=_TypedFailsACheck(),  # type: ignore[arg-type]
        escalate_generator=EchoQueryGenerator(COUNT),
        composer=None,
    )
    assert res.trace.status == "ok", res.trace.error
    assert res.response.sql and "count(*)" in res.response.sql.lower()
    assert res.response.resolution is not None and res.response.resolution.typed is False
    untyped = [n for n in res.response.degraded if n.startswith("UNTYPED: ")]
    assert len(untyped) == 1, res.response.degraded
    assert "typed reading failed a check" in untyped[0]


@needs_db
def test_every_door_carries_the_same_untyped_line_and_resolution_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM(["There are 100 customers."]),
    )
    monkeypatch.setattr(
        query_api.assembly,
        "select_generators",
        lambda *a, **k: (_DoesNotType(), EchoQueryGenerator(COUNT), "none", None),
    )
    # Certified matching cannot run: the door adds its own line to `degraded`.
    _dead_embedder(monkeypatch)
    auth = {"Authorization": f"Bearer {raw}"}
    try:
        # The API.
        r = client.post("/v1/lenses/customer_value/query", json={"q": QUESTION}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        lines = body["degraded"]
        assert any(n.startswith("DEGRADED: certified matching did not run") for n in lines)
        assert any(n.startswith("UNTYPED: served by raw-SQL generation") for n in lines), lines
        tag = body["resolution"]["tag"]
        assert tag and body["resolution"]["typed"] is False

        # The MCP tool, against the same server.
        monkeypatch.setattr(mcp_server, "DST_API_KEY", raw)
        monkeypatch.setattr(
            mcp_server,
            "_client",
            lambda key, agent="mcp": httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://dst.test",
                headers={"Authorization": f"Bearer {key}"},
            ),
        )
        out = asyncio.run(mcp_server.query("customer_value", QUESTION, ctx=None))
        assert out["ok"] is True, out
        assert out["degraded"] == lines
        assert out["resolution"]["tag"] == tag and out["resolution"]["typed"] is False

        # `dst query`, against the same server: every line verbatim, and the tag.
        def post(url: str, headers: Any = None, json: Any = None, **_: Any) -> httpx.Response:
            return client.post(url, headers=headers, json=json)

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(
            sys, "argv", ["dst", "query", "customer_value", QUESTION, "--token", raw]
        )
        from services.cli.main import main

        capsys.readouterr()
        assert main() == 0
        printed = capsys.readouterr().out
        for line in lines:
            assert line in printed, printed
        assert f"resolution: {tag}" in printed

        # The OpenAI-compatible door carries both beside the prose.
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "dst/customer_value",
                "messages": [{"role": "user", "content": QUESTION}],
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
        extras = r.json()["dst"]
        assert extras["degraded"] == lines
        assert extras["resolution"]["tag"] == tag and extras["resolution"]["typed"] is False
    finally:
        _cleanup(settings.database_admin_url, org)
