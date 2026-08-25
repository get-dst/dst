"""MCP tools return a structured envelope — an auth failure and a valid
answer are programmatically distinguishable; certified tools resolve and pin.
Tools must stay async: under the remote transport they run on the API's own event
loop and call back into the same server — a sync tool is a self-deadlock."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import httpx
import pytest

from services.mcp import server as srv


def _transport(routes: dict[str, tuple[int, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        status, body = routes.get(key, (404, {"detail": "no route"}))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _wire(monkeypatch: pytest.MonkeyPatch, routes: dict[str, tuple[int, Any]]) -> None:
    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=_transport(routes), base_url="http://dst.test"
        ),
    )


def test_missing_key_is_a_structured_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "DST_API_KEY", "")
    out = asyncio.run(srv.query("churn", "how many?", ctx=None))
    assert out["ok"] is False and out["code"] == "auth"
    lenses = asyncio.run(srv.list_lenses(ctx=None))
    assert lenses["ok"] is False and lenses["code"] == "auth"


def test_success_and_http_failures_are_distinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        {
            "GET /v1/lenses": (200, [{"name": "churn"}]),
            "POST /v1/lenses/churn/query": (200, {"answer": "42", "request_id": "req_1"}),
            "POST /v1/lenses/secret/query": (403, {"detail": "not permitted"}),
        },
    )
    lenses = asyncio.run(srv.list_lenses(ctx=None))
    assert lenses["ok"] is True and lenses["lenses"][0]["name"] == "churn"
    ok = asyncio.run(srv.query("churn", "how many?", ctx=None))
    assert ok["ok"] is True and ok["answer"] == "42"
    denied = asyncio.run(srv.query("secret", "how many?", ctx=None))
    assert denied["ok"] is False and denied["code"] == "forbidden"
    assert "not permitted" in denied["error"]


def test_run_certified_resolves_question_only_at_exact_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    near = {
        "lens": "churn",
        "certified": [{"cert_id": "c1", "question": "active users?", "score": 0.90, "sql": "S"}],
    }
    _wire(monkeypatch, {"GET /v1/lenses/churn/certified": (200, near)})
    out = asyncio.run(srv.run_certified("churn", ctx=None, question="actives?"))
    assert out["ok"] is False and out["code"] == "no_exact_match"
    assert out["near_matches"][0]["cert_id"] == "c1"

    exact = {
        "lens": "churn",
        "certified": [{"cert_id": "c1", "question": "active users?", "score": 0.97, "sql": "S"}],
    }
    ran = {"answer": "42", "certification": "certified", "trust_summary": "Certified answer"}
    _wire(
        monkeypatch,
        {
            "GET /v1/lenses/churn/certified": (200, exact),
            "POST /v1/lenses/churn/certified/c1/run": (200, ran),
        },
    )
    out = asyncio.run(srv.run_certified("churn", ctx=None, question="active users?"))
    assert out["ok"] is True and out["certification"] == "certified"
    assert out["trust_summary"]


def test_run_certified_pins_by_cert_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"answer": "42", "certification": "certified"}
    _wire(monkeypatch, {"POST /v1/lenses/churn/certified/c9/run": (200, ran)})
    out = asyncio.run(srv.run_certified("churn", ctx=None, cert_id="c9"))
    assert out["ok"] is True and out["certification"] == "certified"
    missing = asyncio.run(srv.run_certified("churn", ctx=None))
    assert missing["ok"] is False


def test_search_certified_passes_question_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q", "")
        return httpx.Response(200, json={"lens": "churn", "certified": []})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )
    out = asyncio.run(srv.search_certified("churn", "who churned?", ctx=None))
    assert out["ok"] is True and out["certified"] == []
    assert captured["q"] == "who churned?"


def test_lookup_definition_relays_hits_and_names_the_empty_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap read door: a definitional question must not become a warehouse
    scan — route_query answers one by generating SQL and scanning thousands of
    rows. An empty result has to say so loudly, or the agent invents a
    definition."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params.get("q", "")
        hits = (
            [{"lens": "churn", "term": "active customer", "body": "…", "cites": ["tenure"]}]
            if captured["q"] == "active customer"
            else []
        )
        return httpx.Response(200, json={"q": captured["q"], "definitions": hits})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )
    out = asyncio.run(srv.lookup_definition("active customer", ctx=None))
    assert out["ok"] is True
    assert out["definitions"][0]["term"] == "active customer"
    assert out["definitions"][0]["cites"] == ["tenure"]

    miss = asyncio.run(srv.lookup_definition("nonsense", ctx=None))
    assert miss["ok"] is True and miss["definitions"] == []
    assert "do not" in miss["note"] and "invent" in miss["note"]


def test_route_query_returns_route_and_decline_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lens-less route_query tool proxies POST /v1/query and faithfully
    relays both the routed answer (with which-lens provenance) and the uncovered
    decline — an agent branches on `covered` without parsing prose."""
    routed = {
        "covered": True,
        "routed_to": {"lens": "customer_value", "score": 0.91},
        "answer": {"lens": "customer_value", "answer": "42", "request_id": "req_1"},
    }
    _wire(monkeypatch, {"POST /v1/query": (200, routed)})
    out = asyncio.run(srv.route_query("how many customers?", ctx=None))
    assert out["ok"] is True and out["covered"] is True
    assert out["routed_to"]["lens"] == "customer_value"
    assert out["answer"]["answer"] == "42"

    declined = {"covered": False, "reason": "no governed lens covers this", "nearest_miss": None}
    _wire(monkeypatch, {"POST /v1/query": (200, declined)})
    out = asyncio.run(srv.route_query("how many tickets?", ctx=None))
    assert out["ok"] is True and out["covered"] is False
    assert "no governed lens covers this" in out["reason"]
    assert "answer" not in out  # never an ungoverned answer relayed


def test_route_query_missing_key_is_a_structured_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(srv, "DST_API_KEY", "")
    out = asyncio.run(srv.route_query("anything", ctx=None))
    assert out["ok"] is False and out["code"] == "auth"


def test_review_status_relays_verdict_and_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent that submitted via send_for_review polls review_status to learn the
    verdict: a resolved ticket relays its state + human_verdict; an unknown id is a
    structured not_found, not an answer-shaped success."""
    resolved = {
        "ticket_id": "rev_abc",
        "state": "approved",
        "human_verdict": "approve",
        "human_reasoning": "checked",
        "url": "http://dst.test/reviews/rev_abc",
    }
    _wire(monkeypatch, {"GET /v1/reviews/rev_abc": (200, resolved)})
    out = asyncio.run(srv.review_status("rev_abc", ctx=None))
    assert out["ok"] is True and out["state"] == "approved"
    assert out["human_verdict"] == "approve"

    _wire(monkeypatch, {"GET /v1/reviews/rev_gone": (404, {"detail": "review not found"})})
    missing = asyncio.run(srv.review_status("rev_gone", ctx=None))
    assert missing["ok"] is False and missing["code"] == "not_found"


def test_envelope_shape_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "DST_API_KEY", "")
    out = asyncio.run(srv.describe_lens("churn", ctx=None))
    json.dumps(out)  # agents consume this directly
    assert set(out) >= {"ok", "code", "error"}


def test_server_ships_operating_manual_as_instructions() -> None:
    """The connecting agent gets the cross-tool operating manual for free: MCP carries the
    server `instructions` in its initialize response, so any client (Claude Code/Desktop,
    Cursor) injects it as system context with zero install. Pin the load-bearing concepts
    so the manual can't silently lose the 'when/what' the tool docstrings don't own."""
    guide = srv.mcp.instructions or ""
    assert guide, "FastMCP must be constructed with instructions= (the operating manual)"
    low = guide.lower()
    for concept in ("lens", "list_lenses", "route_query", "certified", "review", "decline"):
        assert concept in low, concept
    # The agent must DEFER lens selection to dst's router — never hand-pick a lens
    # because one "seems more suitable" (the failure that motivated this manual). Pin both
    # the default-to-route framing and the explicit lens-selection prohibition.
    assert "default to route_query" in low, "route_query must be framed as the default ask"
    assert "lens selection" in low and "router's job" in low, "must forbid the agent picking a lens"
    # The read door: a definitional question must not become a warehouse scan —
    # route_query answers "what do we mean by traded volume?" by generating SQL and
    # scanning the table. The manual is the only place an agent learns to prefer
    # the cheap door, so it must name the tool and the meaning-vs-data split.
    assert "lookup_definition" in low, "the manual must name the read door"
    assert "meaning" in low, "the manual must teach the meaning-vs-data split"


def test_instance_name_speaks_through_the_manual() -> None:
    """A named instance ("ask watson") must present AS that name: the manual speaks
    as it, keeps every pinned concept, and still identifies itself as a dst instance
    so the driver can connect the alias to the brand. The default stays bare "dst"
    with no identity preamble — an unnamed install reads exactly as before."""
    named = srv.build_guide("watson")
    low = named.lower()
    assert "watson answers data questions" in low
    assert '"watson" is this org\'s dst (data serve tool) instance' in low
    for concept in ("lens", "list_lenses", "route_query", "certified", "review", "decline"):
        assert concept in low, concept
    assert "{name}" not in named, "template placeholder leaked into the served manual"
    default = srv.build_guide()
    assert default.startswith("dst answers data questions")
    assert "data serve tool" not in default


def test_instance_name_resolves_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """DST_INSTANCE_NAME follows the DST_URL pattern: process env wins, blank or
    unset falls back to "dst" (never an empty server name)."""
    monkeypatch.setenv("DST_INSTANCE_NAME", "watson")
    assert srv._resolve_instance_name() == "watson"
    monkeypatch.setenv("DST_INSTANCE_NAME", "   ")
    assert srv._resolve_instance_name() == "dst"


def test_getting_started_prompt_is_registered() -> None:
    """An on-demand companion to the always-on manual: a user-invokable prompt
    (/mcp__dst__getting_started) that kicks off a governed-querying session."""
    names = [p.name for p in asyncio.run(srv.mcp.list_prompts())]
    assert "getting_started" in names


def test_answer_format_reaches_the_data_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """`format` is the biggest latency lever an agent has — writing the prose
    dominates a certified request — and it is worth nothing unless the tools
    actually put it on the wire rather than leaving the field unread."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent[request.url.path] = json.loads(request.content)
        return httpx.Response(200, json={"answer": "rows only", "request_id": "req_1"})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )
    asyncio.run(srv.query("churn", "how many?", ctx=None, format="structured"))
    assert sent["/v1/lenses/churn/query"] == {"q": "how many?", "format": "structured"}
    asyncio.run(srv.run_certified("churn", ctx=None, cert_id="c9", format="structured"))
    assert sent["/v1/lenses/churn/certified/c9/run"] == {"bindings": {}, "format": "structured"}
    # ...and the default every existing agent already sends is still prose + rows.
    asyncio.run(srv.route_query("how many?", ctx=None))
    assert sent["/v1/query"] == {"q": "how many?", "format": "both"}


def test_tools_are_async_so_remote_transport_cannot_self_deadlock() -> None:
    """Under streamable HTTP the tools run on the API's own event loop and proxy back
    into the same server; FastMCP calls sync tools inline, so a blocking tool wedges
    the whole API (health included) for the full client timeout. Keep them coroutines.

    Enumerated from the REGISTRY, not by hand: a hand-kept list lets the next tool
    added escape the pin, which is the one thing this test exists to prevent — and
    it already had to be hand-extended once for `sql`.
    """
    registered = [t.name for t in asyncio.run(srv.mcp.list_tools())]
    assert registered, "no tools registered — the pin would pass vacuously"
    for name in registered:
        tool = getattr(srv, name)
        assert inspect.iscoroutinefunction(tool), name


def test_query_metrics_sends_only_the_fields_the_caller_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted arguments must not reach the API as explicit nulls: `entity: null`
    is a different intent from an absent entity, and the compiler reads it."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"sql": "SELECT 1", "data": {"rows": [[1]]}})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )
    out = asyncio.run(srv.query_metrics("churn", ctx=None, metrics=["revenue"]))
    assert out["ok"] is True and out["sql"] == "SELECT 1"
    assert captured == {"format": "structured", "metrics": ["revenue"]}


def test_query_metrics_relays_a_compile_error_as_a_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused fan-out or an unknown metric is a 400; the agent must see a
    structured failure it can branch on, not a payload that looks like success."""
    _wire(monkeypatch, {"POST /v1/lenses/churn/metrics": (400, {"detail": "unknown metric 'x'"})})
    out = asyncio.run(srv.query_metrics("churn", ctx=None, metrics=["x"]))
    assert out["ok"] is False and "unknown metric" in out["error"]


def _capture_reviews(monkeypatch: pytest.MonkeyPatch, sent: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"ticket_id": "rev_1", "state": "open"})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )


def test_send_for_review_routes_a_correction_as_precisely_as_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP door hardcoded `kind: other` and sent no `target`, so an agent's
    correction routed by note-vocabulary matching — worse-placed than the same
    correction filed through `dst correct`, which REQUIRES --kind/--target
    because a target-less placement mistargets: an explicit correction target
    outranks kind. The tool carries both, verbatim, to the same POST /v1/reviews
    the CLI posts to."""
    sent: dict[str, Any] = {}
    _capture_reviews(monkeypatch, sent)
    out = asyncio.run(
        srv.send_for_review(
            "req_9",
            ctx=None,
            note="active_customer conflates active with not-cancelled",
            kind="definition",
            target="active_customer",
            corrected_sql="SELECT ...",
        )
    )
    assert out["ok"] is True and out["ticket_id"] == "rev_1"
    # Byte for byte the correction `dst correct --kind definition --target
    # active_customer …` builds: target present and authoritative, the real kind,
    # not "other". This is what "an agent files as precisely as a human" means.
    assert sent == {
        "request_id": "req_9",
        "correction": {
            "kind": "definition",
            "note": "active_customer conflates active with not-cancelled",
            "target": "active_customer",
            "corrected_sql": "SELECT ...",
        },
    }


def test_send_for_review_without_a_correction_files_only_a_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No note and no SQL is a trust sign-off, not a correction: it carries no
    correction block, so no kind/target the agent never chose is invented for it."""
    sent: dict[str, Any] = {}
    _capture_reviews(monkeypatch, sent)
    asyncio.run(srv.send_for_review("req_9", ctx=None))
    assert sent == {"request_id": "req_9"}
