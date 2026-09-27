"""Public demo mode: every signed-in person is a demo caller in one org.

Pins the mapping on all three doors (bearer data plane, MCP OAuth grant, key
mint), that sign-in never reaches admin, that the self-serve key is one live
key per person, and that none of it exists when DST_DEMO_ORG_ID is unset.
Clerk verification is faked at `verify_token` — a token is not a test fixture.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.oauth import _resolve_grant_identity
from services.auth import clerk, demo
from services.config import instance_name, settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.lens_config import AccessRule
from services.db.session import org_session
from services.governance import ratelimit
from services.lenses import store
from services.lenses.demo import LENS_NAME, jaffle_customer_value_bundle
from services.project import demo_page
from services.project.schema import DemoConfig


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
_admin = create_engine(settings.database_admin_url)

VISITOR = "Visitor@Example.com"
CLAIMS = {"email": VISITOR, "sub": "user_1"}


@pytest.fixture(autouse=True)
def _clean_budget() -> Iterator[None]:
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def client(live_client: TestClient) -> TestClient:
    return live_client


@pytest.fixture
def demo_org(monkeypatch: pytest.MonkeyPatch) -> Iterator[uuid.UUID]:
    """Demo mode on, pointed at a fresh org with the demo lens published to
    group `demo`; Clerk verification faked to one visitor."""
    with _admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('DemoMode') RETURNING id")
        ).scalar_one()
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(group="demo")]
    with org_session(org) as session:
        store.create_lens(session, bundle)
        store.publish(session, LENS_NAME)
    monkeypatch.setattr(settings, "demo_org_id", str(org))
    monkeypatch.setattr(clerk, "verify_token", lambda t: CLAIMS if t == "clerk-ok" else None)
    try:
        yield uuid.UUID(str(org))
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@needs_db
def test_visitor_is_a_demo_caller_in_the_demo_org(demo_org: uuid.UUID) -> None:
    ident = demo.resolve("clerk-ok")
    assert ident is not None
    assert ident.org_id == demo_org
    assert ident.name == VISITOR.lower()
    assert ident.groups == ["demo"]
    assert not ident.is_admin
    # Idempotent: the same person is the same caller row.
    again = demo.resolve("clerk-ok")
    assert again is not None and again.caller_id == ident.caller_id
    with org_session(demo_org) as session:
        rows = session.execute(text("SELECT type FROM caller WHERE name = :n"), {"n": ident.name})
        assert [r[0] for r in rows] == ["demo"]


@needs_db
def test_bearer_data_plane_sees_the_demo_lens(client: TestClient, demo_org: uuid.UUID) -> None:
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth("clerk-ok"))
    assert r.status_code == 200
    assert client.get("/v1/lenses", headers=_auth("clerk-bad")).status_code == 401


@needs_db
def test_sign_in_never_reaches_admin(client: TestClient, demo_org: uuid.UUID) -> None:
    r = client.get("/mgmt/callers", headers=_auth("clerk-ok"))
    assert r.status_code == 403
    assert "public demo" in r.json()["detail"]


@needs_db
def test_mcp_grant_lands_on_the_demo_caller(demo_org: uuid.UUID) -> None:
    resolved = _resolve_grant_identity("clerk-ok")
    assert resolved is not None
    org, cid = resolved
    assert org == demo_org
    ident = demo.resolve("clerk-ok")
    assert ident is not None and ident.caller_id == cid
    assert _resolve_grant_identity("clerk-bad") is None


@needs_db
def test_mint_issues_one_live_key_per_person(client: TestClient, demo_org: uuid.UUID) -> None:
    first = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["caller"] == VISITOR.lower()
    assert body["key"].startswith("dst_")
    assert body["lenses"] == [LENS_NAME]
    # each lens comes with a question its own semantic layer declares, not a fixed one
    assert set(body["examples"]) == {LENS_NAME}
    assert body["expires_in_days"] == settings.demo_key_days
    # each AI client's setup, around the key the mint just issued
    connect = {c["id"]: c for c in body["connect"]}
    assert list(connect) == ["claude", "claude-code", "codex", "chatgpt", "cursor"]
    assert all(f"{body['base_url']}/mcp" in c["code"] for c in connect.values())
    assert all(body["key"] in connect[c]["code"] for c in ("claude-code", "codex", "cursor"))
    # and the one line for the visitor's AI, with the key in it
    assert body["agent_line"] == (
        f"Connect me to {instance_name()}: {body['base_url']}/SKILL.md — my key: {body['key']}"
    )
    assert [o["id"] for o in body["other"]] == ["curl", "openai"]
    assert all(body["key"] in o["code"] for o in body["other"])
    # The minted key is a real caller key on the data plane.
    assert client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(body["key"])).status_code == 200
    # A re-mint retires the previous key.
    second = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert second.status_code == 201
    assert client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(body["key"])).status_code == 401
    assert (
        client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(second.json()["key"])).status_code
        == 200
    )


@needs_db
def test_mint_needs_a_session(client: TestClient, demo_org: uuid.UUID) -> None:
    assert client.post("/auth/demo-key").status_code == 401
    assert client.post("/auth/demo-key", headers=_auth("clerk-bad")).status_code == 401


@needs_db
def test_mint_is_budgeted_per_address(client: TestClient, demo_org: uuid.UUID) -> None:
    from services.api.demo import _MINT_IP_RPM

    for _ in range(_MINT_IP_RPM):
        assert client.post("/auth/demo-key", headers=_auth("clerk-ok")).status_code == 201
    r = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_nothing_exists_outside_demo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_org_id", None)
    assert client.get("/demo").status_code == 404
    assert client.post("/auth/demo-key", headers=_auth("clerk-ok")).status_code == 404
    assert demo.resolve("clerk-ok") is None


def test_page_needs_clerk(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(clerk, "issuer", lambda: None)
    assert client.get("/demo").status_code == 503


def test_page_boots_clerk_under_the_consent_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    r = client.get("/demo")
    assert r.status_code == 200
    assert 'data-clerk-publishable-key="pk_test_x"' in r.text
    assert "ex.clerk.accounts.dev/npm/@clerk/clerk-js" in r.text
    assert "/auth/demo-key" in r.text
    assert r.headers["content-security-policy"].startswith("frame-ancestors 'none'")


def test_every_page_a_person_sees_carries_the_instance_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DST_INSTANCE_NAME names the deployment on the demo page and both consent
    pages, not only to the driver AI; a named deployment credits dst beneath it."""
    from services.api.oauth import _clerk_consent_html, _consent_html

    monkeypatch.setenv("DST_INSTANCE_NAME", "roshan")
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    page = client.get("/demo").text
    assert "<title>roshan · demo</title>" in page
    assert "answers by dst (data serve tool)" in page
    assert "Sign in to roshan" in _clerk_consent_html({}, "pk", "host", "Claude")
    assert "Authorize roshan access" in _consent_html({})

    monkeypatch.delenv("DST_INSTANCE_NAME")
    page = client.get("/demo").text
    assert "<title>dst · demo</title>" in page and "answers by dst" not in page


def test_sign_in_returns_to_the_page_it_started_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a Google/GitHub/Discord round trip Clerk lands the person on the
    mount's redirect props — or, without them, on the instance default, which is
    the site root (the dashboard login), key and consent both lost. Every mount on
    both pages passes the page's own URL, query string kept: on the consent page
    the query is the pending OAuth request."""
    import re

    from services.api.oauth import _clerk_consent_html

    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    pages = {
        "demo": client.get("/demo").text,
        "consent": _clerk_consent_html({"client_id": "c", "state": "s"}, "pk", "host", "Claude"),
    }
    for which, page in pages.items():
        mounts = re.findall(r"Clerk\.mountSignIn\(([^)]*)\)", page)
        assert mounts, which
        for props in mounts:
            assert "forceRedirectUrl: here" in props, which
            assert "signUpForceRedirectUrl: here" in props, which
        assert "const here = location.origin + location.pathname + location.search;" in page


def test_a_demo_oauth_token_lives_as_long_as_a_demo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no refresh token, so a demo client's grant is the token's whole
    life: the week the demo promises, then the person reconnects. Outside demo
    mode the long default stands."""
    from datetime import timedelta

    from services.api.oauth import _token_ttl
    from services.auth import oauth

    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "demo_key_days", 7)
    assert _token_ttl() == timedelta(days=7)
    monkeypatch.setattr(settings, "demo_org_id", None)
    assert _token_ttl() == oauth.OAUTH_TOKEN_TTL


@needs_db
def test_the_page_says_what_the_demo_is_about_before_sign_in(
    client: TestClient, demo_org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visitor sees what they will be able to ask before signing in: each lens a
    demo caller may use, by its display name, with an example question its own
    semantic layer declares. With no `demo:` section applied the page carries
    dst's own sentence and no example."""
    from services.api.demo import TAGLINE, _example

    monkeypatch.setenv("DST_INSTANCE_NAME", "roshan")
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    bundle = jaffle_customer_value_bundle()  # the lens the fixture published
    cfg = bundle.config
    page = client.get("/demo").text
    assert '<h1 class="name">roshan</h1>' in page
    assert TAGLINE in page
    assert "What you can ask" in page
    assert f'<span class="ln">{cfg.display_name or cfg.name}</span>' in page
    assert f'<span class="q">{_example(bundle) or ""}</span>' in page
    assert 'class="chat"' not in page


@needs_db
def test_the_file_an_agent_reads_serves_the_demo(
    client: TestClient, demo_org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /SKILL.md: public markdown, cached briefly, naming the instance and its
    MCP URL, with a section per AI client and the demo's topics from the same
    listing the page uses. The page's one line points at it, without a key until
    sign-in."""
    from services.api.demo import _example, connect_snippets

    monkeypatch.setenv("DST_INSTANCE_NAME", "roshan")
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    r = client.get("/SKILL.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=300"
    md = r.text
    assert md.startswith("# roshan\n")
    assert "MCP URL: http://testserver/mcp" in md
    for item in connect_snippets("http://testserver", "roshan", "<KEY>", None):
        assert f"## {item['label']}\n" in md, item["id"]
    bundle = jaffle_customer_value_bundle()  # the lens the fixture published
    question = _example(bundle)
    assert f"- {bundle.config.display_name}{f' — {question}' if question else ''}\n" in md
    assert "## Limits" in md
    page = client.get("/demo").text
    assert '<pre id="line">Connect me to roshan: http://testserver/SKILL.md</pre>' in page


def test_the_file_an_agent_reads_outside_demo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deployment serves it, from its public address: without one there is
    nothing to point a client at, and the 404 says which setting gives it."""
    monkeypatch.setattr(settings, "demo_org_id", None)
    monkeypatch.setattr(settings, "public_base_url", None)
    r = client.get("/SKILL.md")
    assert r.status_code == 404
    assert "DST_PUBLIC_BASE_URL" in r.json()["detail"]
    monkeypatch.setattr(settings, "public_base_url", "https://dst.example.com/")
    r = client.get("/SKILL.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "MCP URL: https://dst.example.com/mcp" in r.text
    assert "ask the operator of" in r.text
    assert "## Limits" not in r.text and "/demo" not in r.text


@pytest.fixture
def project_demo_org(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[uuid.UUID, dict[str, str]]]:
    """Demo mode on a fresh org that `dst apply` manages (no lens outside the
    project, so an apply has nothing of its own to recompile), with an admin token."""
    from services.auth.tokens import hash_token, new_admin_token

    raw = new_admin_token()
    with _admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('DemoProject') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    monkeypatch.setattr(settings, "demo_org_id", str(org))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    try:
        yield uuid.UUID(str(org)), _auth(raw)
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


_DEMO_YAML = """name: demo
demo:
  tagline: Ask about the shop from any AI you use.
  example:
    - user: Who are our best customers?
    - tool: asked roshan — top customers by lifetime value
    - assistant: Three customers account for a fifth of revenue.
    - receipt:
        zeta: last
        alpha: first
"""


@needs_db
def test_the_demo_section_rides_apply_and_shows_on_the_page(
    client: TestClient, project_demo_org: tuple[uuid.UUID, dict[str, str]]
) -> None:
    """dst.yaml's `demo:` lands through `dst apply` (files win: absent from a
    pushed dst.yaml, it clears), and /demo renders exactly what was applied.
    A malformed section is rejected at plan and aborts apply."""
    _org, admin = project_demo_org

    def apply(yaml_text: str) -> list[dict[str, object]]:
        r = client.post(
            "/mgmt/project/apply", json={"files": {"dst.yaml": yaml_text}}, headers=admin
        )
        assert r.status_code == 200, r.text
        return list(r.json())

    rows = [r for r in apply(_DEMO_YAML) if r.get("scope") == "demo"]
    assert rows == [
        {
            "scope": "demo",
            "applied": ["demo page: tagline, example of 4 turn(s)"],
            "warnings": [],  # this org IS the demo org: nothing stored unseen
            "errors": [],
        }
    ]
    page = client.get("/demo").text
    assert "Ask about the shop from any AI you use." in page
    assert '<div class="u">Who are our best customers?</div>' in page
    # jsonb reorders object keys; the receipt keeps the order it was written in
    assert page.index("<b>zeta</b>") < page.index("<b>alpha</b>")
    # unchanged → silent
    assert not [r for r in apply(_DEMO_YAML) if r.get("scope") == "demo"]

    bad = _DEMO_YAML.replace("- user:", "- usr:")
    plan = client.post("/mgmt/project/plan", json={"files": {"dst.yaml": bad}}, headers=admin)
    assert any(
        r.get("scope") == "project" and "unknown key `usr`" in str(r.get("error"))
        for r in plan.json()
    )
    assert apply(bad)[-1]["action"] == "aborted"
    assert "Who are our best customers?" in client.get("/demo").text  # prior state stands

    cleared = [r for r in apply("name: demo\n") if r.get("scope") == "demo"]
    assert cleared and cleared[0]["applied"] == ["demo page: cleared"]
    assert 'class="chat"' not in client.get("/demo").text


@needs_db
def test_a_demo_section_off_the_demo_org_says_it_is_not_shown(demo_org: uuid.UUID) -> None:
    """Stored either way, but a section this server will never render says so."""
    from services.project import demo_page
    from services.project.schema import DemoConfig

    with org_session(demo_org) as session:
        applied, warnings = demo_page.apply(
            session, DemoConfig.model_validate({"tagline": "x"}), org_id=uuid.uuid4()
        )
    assert applied == ["demo page: tagline"]
    assert warnings and "DST_DEMO_ORG_ID" in warnings[0]


# ── the audience knob: a consumer demo's answers carry no working ────────────


# The sentence as each audience's composer writes it. The pipeline is faked
# here, so the prose is canned: what the door owes is to ask for the right one
# (the `audience` it threads into run_query) and to hand it over whole.
_ENGINEER_PROSE = (
    "There are 19 repeat customers (per the repeat_customer definition: two or more "
    "orders). This covers only customers within the loaded window, and the data is stale "
    "after 2 days per this lens; the NULL rows are customers below the threshold. "
    "(data as of 2026-09-26)"
)
_CONSUMER_PROSE = "19 customers have ordered more than once (as of 26 September)."
# What a consumer's sentence never carries: an identifier, or the system's
# words for its own machinery.
_IDENTIFIER = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
_MACHINERY = re.compile(r"\b(?:definition|lens|window|NULL|threshold)\b", re.IGNORECASE)


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Pin the pipeline's outcome to an answer carrying every block a consumer
    must not see: the working (SQL, rows, citations, checks, the ledger whose
    inferred slots are SQL text) and the trust apparatus meant for the operator
    (grade, certification and its provenance, trust summary, degraded lines,
    receipt, composition). The shaping under test starts where run_query returns.
    Returns the list the fake appends each call's `audience` to."""
    seen: list[str] = []
    from services.api import query as query_api
    from services.contracts.resolution import Resolution, Slot
    from services.contracts.response import (
        CertifiedProvenance,
        Citation,
        DataPayload,
        QueryResponse,
        Receipt,
    )
    from services.contracts.trace import TraceLog
    from services.contracts.verification import VerificationCheck, VerificationReport
    from services.runtime.pipeline import PipelineResult

    ledger = Resolution(
        method="construction",
        slots=[Slot(kind="metric", name="COUNT(SELECT 42)", source="inferred")],
        tag="inferred",
    )

    def fake_run_query(**kw: object) -> PipelineResult:
        rid = f"req-{uuid.uuid4()}"
        lens = str(kw["lens_name"])
        seen.append(str(kw.get("audience")))
        return PipelineResult(
            response=QueryResponse(
                lens=lens,
                answer=_CONSUMER_PROSE if kw.get("audience") == "consumer" else _ENGINEER_PROSE,
                sql="SELECT 42",
                data=DataPayload(columns=["n"], rows=[[42]], row_count=1),
                definition_used="repeat customer: two or more orders",
                citations=[Citation(type="sql", ref="SELECT 42")],
                confidence="verified",
                verification=VerificationReport(
                    grade="verified",
                    checks=[VerificationCheck(name="guard", status="pass", reason="SELECT 42")],
                ),
                certification="certified",
                certified_match="exact",
                certified_provenance=CertifiedProvenance(
                    cert_id="cert_1", certified_by="apply", certified_at="2026-09-20T00:00:00Z"
                ),
                trust_summary="Certified answer — approved by apply on 2026-09-20; served from "
                "approved SQL, no AI generation. Population: all customers.",
                data_as_of="2026-09-26",
                composition="fallback",
                degraded=[
                    "UNTYPED: served by raw-SQL generation — the question did not type",
                    "graded on 6 of 12 checks",
                ],
                receipt=Receipt(
                    request_id=rid,
                    lens=lens,
                    served_at="2026-09-27T00:00:00Z",
                    certification="certified",
                    cert_id="cert_1",
                    confidence="verified",
                    sql_sha256="0" * 64,
                    data_as_of="2026-09-26",
                    resolution_tag="inferred",
                    digest="f" * 64,
                ),
                resolution=ledger,
                request_id=rid,
            ),
            trace=TraceLog(
                request_id=rid,
                org_id=str(kw["org_id"]),
                lens=lens,
                caller=str(kw["caller"]),
                question=str(kw["question"]),
                sql="SELECT 42",
                valid=True,
                row_count=1,
                answer="There are 19 repeat customers.",
                confidence="verified",
                resolution=ledger,
                resolution_tag="inferred",
                status="ok",
            ),
        )

    monkeypatch.setattr(query_api, "run_query", fake_run_query)
    # A provider must resolve before the pipeline is reached; scripted, never dialled.
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: ScriptedLLM([])
    )
    return seen


def _set_audience(org: uuid.UUID, audience: str) -> None:
    with org_session(org) as session:
        demo_page.apply(session, DemoConfig(audience=audience), org_id=org)  # type: ignore[arg-type]


# What a consumer's answer is, on every door: the sentence and what it is, a
# clarification to answer, when the data is from, whether the rows were capped,
# and the id the operator can look the request up by. Nothing else, not as a
# key, a value or a null.
_CONSUMER_KEYS = {
    "lens",
    "status",
    "answer",
    "clarification",
    "data_as_of",
    "truncated",
    "request_id",
}

# The operator's words: the working and the trust apparatus. None may reach a
# consumer's wire on any door, in a key or in a value.
_OPERATOR_WORDS = (
    "SELECT 42",
    "sql",
    "citations",
    "verification",
    "resolution",
    "confidence",
    "verified",
    "partial",
    "certif",
    "Certified answer",
    "trust_summary",
    "degraded",
    "UNTYPED",
    "graded on",
    "receipt",
    "sql_sha256",
    "definition_used",
    "composition",
)


def _assert_nothing_of_the_operators(wire: str) -> None:
    leaked = [word for word in _OPERATOR_WORDS if word in wire]
    assert not leaked, leaked


@needs_db
def test_consumer_audience_keeps_the_sentence_and_drops_the_working(
    client: TestClient, demo_org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`demo.audience: consumer`: a demo caller's answer, on every door, is the
    sentence with its status, clarification, freshness date and request id, and
    not one field of the working or of the trust apparatus (receipt, grade,
    certification, trust summary, degraded lines, ledger), as a key, a value or a
    null — while the request log keeps the whole thing for the operator. The
    sentence itself is composed for that audience: the door threads it into the
    pipeline, and what comes back names no metric, column, definition, lens,
    window or threshold. `engineer` (the default) is the answer as it always was."""
    import asyncio
    import json

    import httpx

    from services.mcp import server as srv

    seen = _fake_pipeline(monkeypatch)
    _set_audience(demo_org, "consumer")
    q = {"q": "how many repeat customers?"}
    r = client.post(f"/v1/lenses/{LENS_NAME}/query", json=q, headers=_auth("clerk-ok"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _CONSUMER_KEYS
    assert seen == ["consumer"]
    assert body["answer"] == _CONSUMER_PROSE
    assert not _IDENTIFIER.search(body["answer"]) and not _MACHINERY.search(body["answer"])
    assert body["status"] == "ok" and body["data_as_of"] == "2026-09-26"
    _assert_nothing_of_the_operators(r.text)
    with _admin.connect() as c:
        logged = c.execute(
            text("SELECT sql, resolution, confidence FROM request_log WHERE request_id = :r"),
            {"r": body["request_id"]},
        ).one()
    assert logged.sql == "SELECT 42" and logged.confidence == "verified"
    assert logged.resolution["tag"] == "inferred"

    # the OpenAI-compatible door: the sentence as the message, the same fields in `dst`
    r = client.post(
        "/v1/chat/completions",
        json={"model": f"dst/{LENS_NAME}", "messages": [{"role": "user", "content": q["q"]}]},
        headers=_auth("clerk-ok"),
    )
    assert r.status_code == 200, r.text
    completion = r.json()
    assert completion["choices"][0]["message"]["content"] == _CONSUMER_PROSE
    assert set(completion["dst"]) == _CONSUMER_KEYS - {"answer"}
    _assert_nothing_of_the_operators(r.text)

    # the MCP tool relays the API body: with a minted demo key, through the real app
    key = client.post("/auth/demo-key", headers=_auth("clerk-ok")).json()["key"]

    def forward(request: httpx.Request) -> httpx.Response:
        proxied = client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        return httpx.Response(proxied.status_code, json=proxied.json())

    monkeypatch.setattr(srv, "DST_API_KEY", key)
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(forward), base_url="http://dst.test"
        ),
    )
    out = asyncio.run(srv.query(LENS_NAME, q["q"], ctx=None))
    assert out["ok"] is True and out["answer"] == _CONSUMER_PROSE
    assert set(out) == {"ok", *_CONSUMER_KEYS}
    _assert_nothing_of_the_operators(json.dumps(out))
    assert seen == ["consumer"] * 3

    _set_audience(demo_org, "engineer")
    body = client.post(f"/v1/lenses/{LENS_NAME}/query", json=q, headers=_auth("clerk-ok")).json()
    assert seen[-1] == "engineer"
    assert body["answer"] == _ENGINEER_PROSE
    assert _IDENTIFIER.search(body["answer"]) and _MACHINERY.search(body["answer"])
    assert body["sql"] == "SELECT 42" and body["data"]["rows"] == [[42]]
    assert body["citations"] == [{"type": "sql", "ref": "SELECT 42"}]
    assert body["confidence"] == "verified" and body["certification"] == "certified"
    assert body["trust_summary"].startswith("Certified answer")
    assert body["receipt"]["sql_sha256"] == "0" * 64
    assert any(line.startswith("UNTYPED") for line in body["degraded"])
    assert body["resolution"]["tag"] == "inferred"


@needs_db
def test_the_consumer_view_is_only_for_demo_callers_in_the_demo_org(
    demo_org: uuid.UUID,
) -> None:
    from services.governance.credentials import CallerIdentity

    _set_audience(demo_org, "consumer")
    visitor = CallerIdentity(org_id=demo_org, name="v", is_admin=False, groups=["demo"])
    assert demo_page.consumer_view(visitor)
    # a caller outside the demo group — a service key in the same org — sees everything
    assert not demo_page.consumer_view(
        CallerIdentity(org_id=demo_org, name="svc", is_admin=False, groups=[])
    )
    # a demo-group caller in some other org: not this deployment's demo
    assert not demo_page.consumer_view(
        CallerIdentity(org_id=uuid.uuid4(), name="v", is_admin=False, groups=["demo"])
    )
    _set_audience(demo_org, "engineer")
    assert not demo_page.consumer_view(visitor)
