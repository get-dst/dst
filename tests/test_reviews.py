"""Review judge (unit) + ticket lifecycle through the API.
A correction records the wrong-vs-right delta on the ticket.
A failed adversarial check pre-fills the ticket's correction."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.correction import CorrectionDelta
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.db.session import org_session
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.mcp import server as mcp_srv
from services.reviews import service
from services.reviews import store as rev_store
from services.reviews.judge import NO_VERDICT, judge_trace
from services.reviews.patch import draft_patch
from services.reviews.store import Trace

client = TestClient(app)


def _trace() -> Trace:
    return Trace(
        request_id="req-1",
        lens="customer_value",
        caller="agent-1",
        question="how many repeat customers?",
        sql="SELECT count(*) FROM customers WHERE number_of_orders > 1",
        answer="There are 19 repeat customers.",
        definition_used="repeat_customer",
        confidence="high",
        row_count=1,
    )


def test_judge_parses_approve() -> None:
    llm = ScriptedLLM(['{"verdict": "approve", "reasoning": "SQL matches the definition."}'])
    verdict, reasoning = judge_trace(llm, _trace())
    assert verdict == "approve"
    assert "definition" in reasoning


def test_judge_handles_bad_json() -> None:
    llm = ScriptedLLM(["not json at all"])
    verdict, _ = judge_trace(llm, _trace())
    assert verdict == "changes"  # escalates on unparseable output


def test_judge_parses_fenced_json() -> None:
    # A small/fast judge model often wraps its object in a markdown fence and a
    # trailing line — that must still parse, not escalate.
    llm = ScriptedLLM(
        ['Here is my verdict:\n```json\n{"verdict": "reject", "reasoning": "invented number"}\n```']
    )
    verdict, reasoning = judge_trace(llm, _trace())
    assert verdict == "reject"
    assert "invented" in reasoning


def test_judge_surfaces_raw_on_unparseable() -> None:
    # When the response truly can't be parsed, the human must see what the judge
    # actually said — not a content-free placeholder.
    llm = ScriptedLLM(["I think the answer is fine but I won't follow the format."])
    verdict, reasoning = judge_trace(llm, _trace())
    assert verdict == "changes"
    assert "I won't follow the format" in reasoning


# ── an empty judge reply is an ERROR, never a ruling ─────────────────────────


@pytest.mark.parametrize("empty", ["", "   \n  "])
def test_empty_judge_response_is_no_verdict_not_changes(
    empty: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty judge reply used to record verdict `changes` with the reasoning
    "(empty judge response)" — a provider outage manufactured into governance
    rulings. Nothing back means UNRULED."""
    llm = ScriptedLLM([empty])
    with caplog.at_level("ERROR", logger="dst"):
        verdict, reasoning = judge_trace(llm, _trace())
    assert verdict == NO_VERDICT == ""
    assert verdict != "changes"
    assert not verdict  # falsy: `verdict == "approve"` can never pass on it
    assert "empty" in reasoning.lower()
    # …and it is loud: an outage that silently unrules every ticket is the same bug
    assert any(r.levelname == "ERROR" and "EMPTY" in r.message for r in caplog.records)


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


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('RevTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_trace(
    org: object,
    request_id: str,
    verification: str | None = None,
    caller: str = "agent-1",
) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, latency, status, "
                "verification) VALUES "
                "(:o, :r, 'customer_value', :c, 'how many repeat customers?', "
                "'SELECT count(*) FROM customers WHERE number_of_orders > 1', true, 1, "
                "'There are 19 repeat customers.', 'repeat_customer', 'high', '{}'::jsonb, 'ok', "
                "CAST(:v AS jsonb))"
            ),
            {"o": org, "r": request_id, "v": verification, "c": caller},
        )


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM patch_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM review WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM api_key WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM caller WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_review_lifecycle_escalate_then_human_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    # AI judge requests changes -> escalates to needs_human
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(
            ['{"verdict": "changes", "reasoning": "double-check the filter"}']
        ),
    )
    try:
        # send for review (admin token acts as a caller)
        r = client.post("/v1/reviews", headers=h, json={"request_id": rid})
        assert r.status_code == 201, r.text
        ticket = r.json()
        assert ticket["state"] == "needs_human"
        assert ticket["ai_verdict"] == "changes"
        assert ticket["origin"] == "human"  # caller-raised, not auto-flagged
        assert ticket["url"].endswith(f"/reviews/{ticket['ticket_id']}")
        # A verdict is evidence someone stands behind: the judge's model name
        # rides the ticket (0050), so the queue can say WHO said "changes".
        assert ticket["judged_by"]
        tid = ticket["ticket_id"]

        # appears in the data-team queue
        q = client.get("/mgmt/reviews?state=needs_human", headers=h)
        assert q.status_code == 200
        assert any(t["ticket_id"] == tid for t in q.json())

        # human rules approve -> resolved
        ruled = client.post(
            f"/mgmt/reviews/{tid}/rule", headers=h, json={"verdict": "approve", "reasoning": "ok"}
        )
        assert ruled.status_code == 200
        assert ruled.json()["state"] == "approved"
        # Raw admin token: recorded as token:<label>, never as a person —
        # the human: prefix is reserved for a verified dashboard session.
        assert ruled.json()["ruled_by"] == "token:t"

        # status readback via data plane
        back = client.get(f"/v1/reviews/{tid}", headers=h)
        assert back.status_code == 200 and back.json()["human_verdict"] == "approve"
    finally:
        _cleanup(org)


@needs_db
def test_review_auto_approves_on_confident_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "approve", "reasoning": "sound"}']),
    )
    try:
        r = client.post("/v1/reviews", headers=h, json={"request_id": rid})
        assert r.status_code == 201
        assert r.json()["state"] == "approved"
    finally:
        _cleanup(org)


@needs_db
def test_empty_judge_leaves_the_ticket_unruled(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the ticket escalates with NO ai_verdict at all — the
    queue must not show the judge as having found fault when it never answered, and
    the unruled row must not enter judge calibration as a disagreement."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: ScriptedLLM([""])
    )
    try:
        ticket = client.post("/v1/reviews", headers=h, json={"request_id": rid}).json()
        assert ticket["state"] == "needs_human"
        assert not ticket["ai_verdict"]
        assert "empty response" in ticket["ai_reasoning"]
        # An outage never claims a ruling: no verdict, no actor.
        assert ticket["judged_by"] == ""
        with org_session(org) as s:  # type: ignore[arg-type]
            rev_store.set_human_ruling(s, ticket["ticket_id"], "approve", "fine")
    finally:
        _cleanup(org)


@needs_db
def test_review_unknown_request_404() -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post("/v1/reviews", headers=h, json={"request_id": "nope"})
        assert r.status_code == 404
    finally:
        _cleanup(org)


# ─── origin: who raised the ticket (0029) ────────────────────────────────────


@needs_db
def test_origin_round_trips_through_store() -> None:
    """origin lands in the review row and reads back on every store surface;
    unstamped tickets are human-raised by definition (the column default)."""
    org, _raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    try:
        with org_session(org) as session:
            trace = rev_store.get_trace(session, rid)
            assert trace is not None
            auto = rev_store.create_ticket(session, trace, origin="ai")
            human = rev_store.create_ticket(session, trace)
            assert auto.origin == "ai" and human.origin == "human"
        with org_session(org) as session:
            fetched = rev_store.get_ticket(session, auto.ticket_id)
            assert fetched is not None and fetched.origin == "ai"
            by_ticket = {t.ticket_id: t.origin for t in rev_store.list_queue(session)}
        assert by_ticket[auto.ticket_id] == "ai"
        assert by_ticket[human.ticket_id] == "human"
    finally:
        _cleanup(org)


# ─── correction capture ──────────────────────────────────────────────────────


@needs_db
def test_correction_round_trips_through_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied correction persists on the ticket and reads back everywhere."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "changes", "reasoning": "caller disputes"}']),
    )
    correction = {
        "kind": "definition",
        "note": "repeat customer should mean 2+ orders in the last 12 months",
        "corrected_sql": "SELECT count(*) FROM customers WHERE number_of_orders >= 2",
        "target": "repeat_customer",  # explicit placement rides the same delta
    }
    try:
        r = client.post(
            "/v1/reviews", headers=h, json={"request_id": rid, "correction": correction}
        )
        assert r.status_code == 201, r.text
        ticket = r.json()
        assert ticket["correction"]["kind"] == "definition"
        assert ticket["correction"]["corrected_sql"].endswith(">= 2")
        tid = ticket["ticket_id"]

        # readback: data plane + mgmt queue both carry the delta
        back = client.get(f"/v1/reviews/{tid}", headers=h).json()
        assert back["correction"]["note"].startswith("repeat customer")
        assert back["correction"]["target"] == "repeat_customer"  # persisted on the ticket
        queue = client.get("/mgmt/reviews", headers=h).json()
        mine = next(t for t in queue if t["ticket_id"] == tid)
        assert mine["correction"]["kind"] == "definition"
    finally:
        _cleanup(org)


@needs_db
def test_human_ruling_attaches_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer can record the delta while ruling (the Reviews UI form)."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "changes", "reasoning": "check"}']),
    )
    try:
        tid = client.post("/v1/reviews", headers=h, json={"request_id": rid}).json()["ticket_id"]
        ruled = client.post(
            f"/mgmt/reviews/{tid}/rule",
            headers=h,
            json={
                "verdict": "changes",
                "reasoning": "scope is wrong",
                "correction": {"kind": "scope", "note": "internal accounts must be excluded"},
            },
        )
        assert ruled.status_code == 200, ruled.text
        assert ruled.json()["correction"]["kind"] == "scope"
        # a later ruling without a correction must not erase the recorded one
        again = client.post(
            f"/mgmt/reviews/{tid}/rule", headers=h, json={"verdict": "reject", "reasoning": ""}
        )
        assert again.json()["correction"]["note"] == "internal accounts must be excluded"
    finally:
        _cleanup(org)


def test_mcp_send_for_review_forwards_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP tool forwards note/corrected_sql as a correction (and stays async)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"ticket_id": "rev_x", "state": "open"})

    monkeypatch.setattr(mcp_srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        mcp_srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )
    out = asyncio.run(
        mcp_srv.send_for_review("req_1", ctx=None, note="wrong filter", corrected_sql="SELECT 2")
    )
    assert out["ok"] is True
    sent = captured["json"]
    assert sent["request_id"] == "req_1"
    assert sent["correction"]["note"] == "wrong filter"
    assert sent["correction"]["corrected_sql"] == "SELECT 2"

    # without a delta the body stays a plain review request (no empty correction)
    out = asyncio.run(mcp_srv.send_for_review("req_2", ctx=None))
    assert out["ok"] is True
    assert "correction" not in captured["json"]


@needs_db
def test_mcp_review_status_reads_verdict_through_live_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E: the review_status MCP tool — run as real code, no mock transport — reaches the
    live API route → caller auth → org_session → Postgres and reads a resolved ticket's
    verdict back; an unknown id surfaces the structured not_found envelope, not an answer."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "approve", "reasoning": "sound"}']),
    )
    # Point the tool's HTTP client at the in-process ASGI app, authed with the real token —
    # so review_status exercises the genuine wire path, not a hand-rolled response.
    monkeypatch.setattr(mcp_srv, "DST_API_KEY", raw)
    monkeypatch.setattr(
        mcp_srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        ),
    )
    try:
        tid = client.post(
            "/v1/reviews", headers={"Authorization": f"Bearer {raw}"}, json={"request_id": rid}
        ).json()["ticket_id"]

        out = asyncio.run(mcp_srv.review_status(tid, ctx=None))
        assert out["ok"] is True, out
        assert out["state"] == "approved"  # auto-approved by the confident judge
        assert out["ticket_id"] == tid
        assert out["url"].endswith(f"/reviews/{tid}")

        missing = asyncio.run(mcp_srv.review_status("rev_nope", ctx=None))
        assert missing["ok"] is False and missing["code"] == "not_found"
    finally:
        _cleanup(org)


# ─── challenge → correction handoff ──────────────────────────────────────────

_CHALLENGE_REASON = "active customers: internal/test accounts were not excluded"


def _challenge_verification(status: str = "fail") -> dict[str, object]:
    """request_log.verification as fold_challenges serializes it."""
    return {
        "grade": "partial",
        "checks": [
            {"name": "numeric_grounding", "status": "pass", "reason": None},
            {"name": "adversary", "status": status, "reason": _CHALLENGE_REASON},
        ],
    }


def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_review without Postgres: create_ticket mirrors the real return value."""
    monkeypatch.setattr(
        rev_store,
        "create_ticket",
        lambda session, trace, correction=None, origin="human": rev_store.Ticket(
            ticket_id="rev_fake",
            request_id=trace.request_id,
            lens=trace.lens,
            caller=trace.caller,
            state="open",
            correction=correction,
            origin=origin,
        ),
    )
    monkeypatch.setattr(rev_store, "set_ai_result", lambda *a, **k: None)


def test_failed_challenge_prefills_scope_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A challenge-carrying trace opens as a correction, not just a warning."""
    _fake_store(monkeypatch)
    trace = _trace().model_copy(update={"verification": _challenge_verification()})
    ticket = service.open_review(None, trace, llm=None)  # type: ignore[arg-type]
    assert ticket.correction is not None
    assert ticket.correction.kind == "scope"
    assert ticket.correction.note == _CHALLENGE_REASON


def test_caller_correction_wins_over_challenge_prefill(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_store(monkeypatch)
    trace = _trace().model_copy(update={"verification": _challenge_verification()})
    mine = CorrectionDelta(kind="definition", note="active means paid within the last 30 days")
    ticket = service.open_review(None, trace, llm=None, correction=mine)  # type: ignore[arg-type]
    assert ticket.correction == mine


def test_passing_or_absent_adversary_check_does_not_prefill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_store(monkeypatch)
    passed = _trace().model_copy(update={"verification": _challenge_verification("pass")})
    assert service.open_review(None, passed, llm=None).correction is None  # type: ignore[arg-type]
    assert service.open_review(None, _trace(), llm=None).correction is None  # type: ignore[arg-type]


def test_prefilled_ticket_drafts_patch_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop closes: challenge → scope correction → drafted, approvable patch."""
    _fake_store(monkeypatch)
    trace = _trace().model_copy(update={"verification": _challenge_verification()})
    ticket = service.open_review(None, trace, llm=None)  # type: ignore[arg-type]
    llm = ScriptedLLM(['{"instruction": "Exclude internal and test accounts unless asked."}'])
    out = draft_patch(llm, ticket, trace, jaffle_customer_value_bundle(), [])
    assert out is not None
    assert out.kind == "instruction"  # a scope correction routes to a lens instruction
    assert out.diff_after == "Exclude internal and test accounts unless asked."
    assert out.ticket_id == ticket.ticket_id
    assert out.status == "candidate"


@needs_db
def test_challenge_trace_prefills_via_api_and_drafts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full path on Postgres: a challenged trace → review pre-filled with the scope
    correction → the draft-patch endpoint emits an approvable instruction candidate."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid, verification=json.dumps(_challenge_verification()))
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # One shared script: the judge consumes the first response, the drafter the second.
    llm = ScriptedLLM(
        [
            '{"verdict": "changes", "reasoning": "challenge unresolved"}',  # judge
            '{"instruction": "Exclude internal and test accounts unless asked."}',  # drafter
        ]
    )
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: llm)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda ref: ResolvedModel(llm, "fake", registry.wire_name(ref)),
    )
    try:
        body = jaffle_customer_value_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=body, headers=h).status_code == 201

        ticket = client.post("/v1/reviews", headers=h, json={"request_id": rid}).json()
        assert ticket["correction"]["kind"] == "scope"
        assert ticket["correction"]["note"] == _CHALLENGE_REASON

        drafted = client.post(f"/mgmt/reviews/{ticket['ticket_id']}/draft-patch", headers=h)
        assert drafted.status_code == 201, drafted.text
        cand = drafted.json()
        assert cand["kind"] == "instruction"
        assert cand["diff_after"] == "Exclude internal and test accounts unless asked."
        assert cand["status"] == "candidate"
    finally:
        _cleanup(org)


# ─── the data plane is caller-scoped ─────────────────────────────────────────
#
# `POST /v1/reviews` was org-scoped and nothing more: any caller key could file a
# correction against — and read the verdict on — any OTHER caller's question and
# answer. Opening the door to the business user who actually finds wrong answers
# means closing that, and closing it HERE: a client-side rule is not a boundary,
# since the same key reaches the same endpoint with curl.


def _key_for(org: object, name: str) -> str:
    from services.governance import credentials

    with org_session(org) as s:  # type: ignore[arg-type]
        return credentials.issue_key(s, credentials.create_caller(s, name, "user"))


@pytest.fixture
def _confident_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "approve", "reasoning": "sound"}'] * 4),
    )


@needs_db
@pytest.mark.usefixtures("_confident_judge")
def test_caller_key_files_and_reads_its_own_correction() -> None:
    """The whole journey a non-admin caller has to be able to take: a dst_ key,
    no admin token anywhere, from wrong answer to filed ticket to its status."""
    org, _admin = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    try:
        _seed_trace(org, rid, caller="peder-holm")
        h = {"Authorization": f"Bearer {_key_for(org, 'peder-holm')}"}

        filed = client.post(
            "/v1/reviews",
            headers=h,
            json={
                "request_id": rid,
                "correction": {
                    "kind": "definition",
                    "target": "active_customer",
                    "note": "the tool conflates fulfilled with not-cancelled",
                },
            },
        )
        assert filed.status_code == 201, filed.text
        tid = filed.json()["ticket_id"]

        # …and he can see what became of it, by id and in his own list.
        back = client.get(f"/v1/reviews/{tid}", headers=h)
        assert back.status_code == 200 and back.json()["ticket_id"] == tid
        mine = client.get("/v1/reviews", headers=h)
        assert mine.status_code == 200
        assert [t["ticket_id"] for t in mine.json()] == [tid]
    finally:
        _cleanup(org)


@needs_db
@pytest.mark.usefixtures("_confident_judge")
def test_a_caller_key_cannot_touch_another_callers_request() -> None:
    """The negatives, all four, on one pair of keys."""
    org, _admin = _make_org_token()
    hers, his = f"req-{uuid.uuid4()}", f"req-{uuid.uuid4()}"
    try:
        _seed_trace(org, hers, caller="marta-lindqvist")
        _seed_trace(org, his, caller="peder-holm")
        peder = {"Authorization": f"Bearer {_key_for(org, 'peder-holm')}"}
        marta_key = _key_for(org, "marta-lindqvist")

        # 1. he cannot file against her request
        blocked = client.post("/v1/reviews", headers=peder, json={"request_id": hers})
        assert blocked.status_code == 403, blocked.text
        assert "their own requests" in blocked.json()["detail"]
        # the refusal does NOT name the other caller
        assert "marta" not in blocked.json()["detail"]

        # 2. he cannot read her ticket, even holding its id
        her_ticket = client.post(
            "/v1/reviews",
            headers={"Authorization": f"Bearer {marta_key}"},
            json={"request_id": hers},
        )
        assert her_ticket.status_code == 201, her_ticket.text
        hers_tid = her_ticket.json()["ticket_id"]
        assert client.get(f"/v1/reviews/{hers_tid}", headers=peder).status_code == 403

        # 3. his own list shows only his own — her ticket is not in it
        mine_tid = client.post("/v1/reviews", headers=peder, json={"request_id": his}).json()[
            "ticket_id"
        ]
        listed = client.get("/v1/reviews", headers=peder).json()
        assert [t["ticket_id"] for t in listed] == [mine_tid]

        # 4. he cannot rule on anything, his own ticket included, nor reach any
        #    admin verb — filing is a report, not a ruling.
        for method, path in [
            ("post", f"/mgmt/reviews/{mine_tid}/rule"),
            ("post", f"/mgmt/reviews/{mine_tid}/draft-patch"),
            ("get", "/mgmt/reviews"),
            ("post", "/mgmt/reviews/patches/pc_1/approve"),
        ]:
            call = getattr(client, method)
            r = (
                call(path, headers=peder, json={"verdict": "approve"})
                if method == "post"
                else call(path, headers=peder)
            )
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"
    finally:
        _cleanup(org)


@needs_db
@pytest.mark.usefixtures("_confident_judge")
def test_an_admin_still_files_on_anyones_request() -> None:
    """The cross-party loop: the analyst holds an admin
    token and triages tickets on the business user's answers. Scoping the caller
    door must not demote her."""
    org, raw = _make_org_token()
    rid = f"req-{uuid.uuid4()}"
    try:
        _seed_trace(org, rid, caller="peder-holm")
        h = {"Authorization": f"Bearer {raw}"}
        filed = client.post("/v1/reviews", headers=h, json={"request_id": rid})
        assert filed.status_code == 201, filed.text
        # the ticket is recorded against the person who ASKED, so it reaches his
        # own list — he is the one waiting to hear the number was fixed
        assert filed.json()["caller"] == "peder-holm"
        peder = {"Authorization": f"Bearer {_key_for(org, 'peder-holm')}"}
        assert [t["ticket_id"] for t in client.get("/v1/reviews", headers=peder).json()] == [
            filed.json()["ticket_id"]
        ]
    finally:
        _cleanup(org)


@needs_db
@pytest.mark.usefixtures("_confident_judge")
def test_mcp_send_for_review_is_caller_scoped_too() -> None:
    """Agents are callers. The MCP tools already reached this endpoint with a
    caller key — so they inherit the scoping, and must."""
    org, _admin = _make_org_token()
    mine, theirs = f"req-{uuid.uuid4()}", f"req-{uuid.uuid4()}"
    try:
        _seed_trace(org, mine, caller="agent-a")
        _seed_trace(org, theirs, caller="agent-b")
        h = {"Authorization": f"Bearer {_key_for(org, 'agent-a')}"}
        assert client.post("/v1/reviews", headers=h, json={"request_id": mine}).status_code == 201
        assert client.post("/v1/reviews", headers=h, json={"request_id": theirs}).status_code == 403
    finally:
        _cleanup(org)
