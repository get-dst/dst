"""Caller keys, per-lens allow-list (deny-by-default), audit."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.lens_config import AccessConfig, AccessRule, LensConfig
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
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


def test_policy_authorize() -> None:
    cfg = LensConfig(
        name="x",
        display_name="X",
        connections=["jaffle"],
        access=AccessConfig(allow=[AccessRule(caller="a")]),
    )
    admin = CallerIdentity(org_id=uuid.uuid4(), name="admin", is_admin=True)
    a = CallerIdentity(org_id=uuid.uuid4(), name="a", is_admin=False)
    b = CallerIdentity(org_id=uuid.uuid4(), name="b", is_admin=False)
    assert authorize(admin, cfg)[0] is True
    assert authorize(a, cfg)[0] is True
    assert authorize(b, cfg)[0] is False


def _org_admin() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Gov') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


@needs_db
def test_caller_allow_list_and_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    org, admin_raw = _org_admin()
    ah = {"Authorization": f"Bearer {admin_raw}"}

    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(caller="agent1")]

    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # Two answered queries here (the REST one and the CLI one), and the provider is
    # built once — ScriptedLLM repeats its LAST entry, so a 2-entry script hands the
    # second query the composed prose as its SQL.
    generate = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM([generate, "ok.", generate, "ok."]),
    )
    try:
        assert (
            client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah).status_code
            == 201
        )
        assert client.post("/mgmt/lenses/customer_value/publish", headers=ah).status_code == 200
        assert client.post("/mgmt/callers", json={"name": "agent1"}, headers=ah).status_code == 201
        assert client.post("/mgmt/callers", json={"name": "other"}, headers=ah).status_code == 201
        k1 = client.post("/mgmt/callers/agent1/keys", headers=ah).json()["key"]
        k2 = client.post("/mgmt/callers/other/keys", headers=ah).json()["key"]

        # allowed caller -> 200
        r1 = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {k1}"},
        )
        assert r1.status_code == 200, r1.text

        # not-allowed caller -> the uniform 404: a denial must be
        # indistinguishable from absence, or lens names become enumerable
        r2 = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {k2}"},
        )
        assert r2.status_code == 404

        # invalid key -> 401
        r3 = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "x"},
            headers={"Authorization": "Bearer dst_invalid"},
        )
        assert r3.status_code == 401

        # The same two verdicts through the product's own door. An admin token bypasses
        # every allow-list, so without `query --key` a granted caller can only be proved
        # with hand-rolled HTTP. The verification loop closes inside the CLI.
        import sys
        from urllib.parse import urlsplit

        import httpx

        from services.cli.main import main as cli_main

        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, headers=None, json=None, timeout=None, params=None: client.post(
                urlsplit(url).path, headers=headers, json=json
            ),
        )
        ask = ["query", "customer_value", "how many customers?", "--key"]
        monkeypatch.setattr(sys, "argv", ["dst", *ask, k1])
        assert cli_main() == 0
        monkeypatch.setattr(sys, "argv", ["dst", *ask, k2])
        assert cli_main() == 1

        # audit recorded a deny for 'other'
        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            denies = c.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE org_id = :o AND caller = 'other' AND decision = 'deny'"
                ),
                {"o": org},
            ).scalar()
        assert denies is not None and denies >= 1
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})  # cascades


def test_everyone_group_admits_any_caller() -> None:
    from services.contracts.lens_config import AccessRule, LensConfig
    from services.governance.credentials import CallerIdentity
    from services.governance.policy import authorize

    cfg = LensConfig(name="l", display_name="l", access={"allow": [AccessRule(group="everyone")]})
    nobody = CallerIdentity(org_id=uuid.uuid4(), name="stranger", groups=[], is_admin=False)
    ok, reason = authorize(nobody, cfg)
    assert ok and "everyone" in reason
