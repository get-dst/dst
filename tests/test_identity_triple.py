"""The identity triple: who, through what, and the join between decision and query.

A governed query records the person (`caller`), the acting client (`agent`), and a
`request_id` that joins the audit decision to the served answer's request_log row.
Before this, `caller` was a lone string and `audit_log` had no request_id, so
"person X, through agent Y, was allowed on lens L and ran SQL S" could not be
reconstructed from the two tables — which is the attribution question a security
team asks the moment an LLM is writing the SQL.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.lens_config import AccessRule
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


def _org_admin() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Triple') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


@needs_db
def test_governed_query_records_principal_agent_and_a_joinable_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, admin_raw = _org_admin()
    ah = {"Authorization": f"Bearer {admin_raw}"}

    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(caller="antti")]
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    gen = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM([gen, "ok."]),
    )
    eng = create_engine(settings.database_admin_url)
    try:
        assert (
            client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah).status_code
            == 201
        )
        assert client.post("/mgmt/lenses/customer_value/publish", headers=ah).status_code == 200
        assert client.post("/mgmt/callers", json={"name": "antti"}, headers=ah).status_code == 201
        key = client.post("/mgmt/callers/antti/keys", headers=ah).json()["key"]

        # A governed query, with the acting client naming itself the way the MCP
        # server does when it proxies in.
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {key}", "X-dst-Agent": "claude-desktop"},
        )
        assert r.status_code == 200, r.text
        rid = r.json()["request_id"]

        # request_log: principal and agent are separate, and the row carries the rid
        # the caller was handed.
        with eng.connect() as c:
            row = c.execute(
                text("SELECT caller, agent FROM request_log WHERE org_id = :o AND request_id = :r"),
                {"o": org, "r": rid},
            ).first()
        assert row is not None, "no request_log row for the served request_id"
        assert row[0] == "antti", "principal (person) not recorded"
        assert row[1] == "claude-desktop", "agent (acting client) not recorded, or not separate"

        # audit_log: the allow decision joins to that same request_id — the whole
        # point. "antti, via claude-desktop, allowed on customer_value, ran <sql>"
        # is now one join across the two tables.
        with eng.connect() as c:
            audit = c.execute(
                text(
                    "SELECT caller, agent, decision FROM audit_log "
                    "WHERE org_id = :o AND request_id = :r AND decision = 'allow'"
                ),
                {"o": org, "r": rid},
            ).first()
        assert audit is not None, "audit_log allow row does not share the request_id"
        assert audit[0] == "antti" and audit[1] == "claude-desktop"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})  # cascades


@needs_db
def test_a_denied_query_still_carries_its_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deny has no request_log row, but its audit row still records a request_id —
    null-joinable and honest, not a silent gap in the trail."""
    org, admin_raw = _org_admin()
    ah = {"Authorization": f"Bearer {admin_raw}"}
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(caller="somebody-else")]
    eng = create_engine(settings.database_admin_url)
    try:
        client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah)
        client.post("/mgmt/lenses/customer_value/publish", headers=ah)
        client.post("/mgmt/callers", json={"name": "outsider"}, headers=ah)
        key = client.post("/mgmt/callers/outsider/keys", headers=ah).json()["key"]

        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {key}", "X-dst-Agent": "cursor"},
        )
        # The uniform 404: a denial must be indistinguishable from absence on
        # the caller surface — the audit row below still records the TRUE
        # reason.
        assert r.status_code == 404

        with eng.connect() as c:
            deny = c.execute(
                text(
                    "SELECT request_id, agent FROM audit_log "
                    "WHERE org_id = :o AND caller = 'outsider' AND decision = 'deny'"
                ),
                {"o": org},
            ).first()
        assert deny is not None
        assert deny[0] is not None, "a deny recorded no request_id"
        assert deny[1] == "cursor", "the acting client is recorded even on a deny"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
