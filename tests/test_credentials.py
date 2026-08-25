"""Caller key listing + revocation actually blocks access."""

from __future__ import annotations

import uuid
from datetime import UTC

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


@needs_db
def test_key_revocation_blocks_query(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Keys') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    ah = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(caller="agent1")]
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(
            ['{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}', "ok."]
        ),
    )
    try:
        client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah)
        client.post("/mgmt/lenses/customer_value/publish", headers=ah)
        client.post("/mgmt/callers", json={"name": "agent1"}, headers=ah)
        key = client.post("/mgmt/callers/agent1/keys", headers=ah).json()["key"]
        kh = {"Authorization": f"Bearer {key}"}

        # works before revocation
        assert (
            client.post("/v1/lenses/customer_value/query", json={"q": "n?"}, headers=kh).status_code
            == 200
        )

        keys = client.get("/mgmt/callers/agent1/keys", headers=ah).json()
        assert len(keys) == 1 and keys[0]["revoked"] is False
        key_id = keys[0]["id"]

        assert client.delete(f"/mgmt/callers/agent1/keys/{key_id}", headers=ah).status_code == 204

        # revoked -> rejected
        assert (
            client.post("/v1/lenses/customer_value/query", json={"q": "n?"}, headers=kh).status_code
            == 401
        )
        assert client.get("/mgmt/callers/agent1/keys", headers=ah).json()[0]["revoked"] is True
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})  # cascades


def _org_with_caller(admin, org_name: str, caller: str) -> tuple[str, str]:
    """An org holding one caller with one active key. Returns (org_id, raw admin token)."""
    from services.db.session import org_session
    from services.governance import credentials

    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": org_name}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        credentials.issue_key(s, credentials.create_caller(s, caller))
        s.commit()
    return oid, raw


def _active_keys(admin, org_id: str, caller: str) -> int:
    with admin.connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM api_key k JOIN caller c ON c.id = k.caller_id "
                    "WHERE c.name = :n AND k.org_id = :o AND k.revoked_at IS NULL"
                ),
                {"n": caller, "o": org_id},
            ).scalar_one()
        )


@needs_db
def test_revoke_key_is_org_scoped_and_names_the_org(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dst revoke-key --caller alex` run from ONE project must not revoke
    `alex` in EVERY org in the database — an access-control operation silently
    reaching across a tenant boundary, from a verb that names no org on the way
    in or out.

    Both doors are pinned here: `--org <name>`, and the org this project's
    DST_ADMIN_TOKEN authenticates as — plus the neighbour's key surviving,
    which is the whole point, and the org named in the output."""
    import argparse

    from services.cli.main import _revoke_key

    admin = create_engine(settings.database_admin_url)
    a_id, a_token = _org_with_caller(admin, "RevokeA", "alex")
    b_id, _b_token = _org_with_caller(admin, "RevokeB", "alex")
    try:
        assert _active_keys(admin, a_id, "alex") == 1
        assert _active_keys(admin, b_id, "alex") == 1

        # 1. `--org` names the scope explicitly.
        monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
        rc = _revoke_key(argparse.Namespace(caller="alex", org="RevokeA", dir="."))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "revoked 1 active key(s) for caller 'alex' in org 'RevokeA'" in out
        assert _active_keys(admin, a_id, "alex") == 0
        assert _active_keys(admin, b_id, "alex") == 1, "revoke crossed a tenant boundary"

        # 2. …and with no --org, the org this project's admin token belongs to.
        monkeypatch.setenv("DST_ADMIN_TOKEN", _b_token)
        rc = _revoke_key(argparse.Namespace(caller="alex", org=None, dir="."))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "in org 'RevokeB'" in out
        assert _active_keys(admin, b_id, "alex") == 0

        # 3. Unscopable = refused, never a database-wide sweep.
        monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
        rc = _revoke_key(argparse.Namespace(caller="alex", org=None, dir="."))
        assert rc == 1
        assert "no org" in capsys.readouterr().err
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id IN (:a, :b)"), {"a": a_id, "b": b_id})


@needs_db
def test_revoke_token_kills_one_credential_of_either_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`dst revoke-token <raw>` — containing a leaked credential without SQL.

    Without the verb, the remedy for a leaked `dstadm_` token is "revoke by
    hash": hand-written SQL, at the worst possible moment. This asserts the
    three outcomes an operator containing a leak needs to tell apart — killed
    it, it was already dead, wrong database — because "no such credential" and
    "already revoked" mean opposite things about whether you are done.
    """
    import argparse

    from services.auth.tokens import new_caller_key
    from services.cli.main import _revoke_token
    from services.db.session import org_session
    from services.governance import credentials

    admin = create_engine(settings.database_admin_url)
    admin_raw = new_admin_token()
    org_name = f"RevokeTok-{uuid.uuid4().hex[:8]}"
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": org_name}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(admin_raw)},
        )
    with org_session(oid) as s:
        caller_raw = credentials.issue_key(s, credentials.create_caller(s, "leaky"))
        s.commit()

    def run(tok: str) -> tuple[int, str, str]:
        rc = _revoke_token(argparse.Namespace(token=tok, dir="."))
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    try:
        # 1. An admin token — the F1 shape, and the one revoke-key cannot reach.
        rc, out, _ = run(admin_raw)
        assert rc == 0, out
        assert f"revoked 1 admin_token in org '{org_name}'" in out

        # 2. A caller key — the F2/F3 shape. Same verb, no prefix knowledge required.
        rc, out, _ = run(caller_raw)
        assert rc == 0, out
        assert f"revoked 1 api_key in org '{org_name}'" in out
        assert _active_keys(admin, oid, "leaky") == 0

        # 3. Idempotent, and says so: re-running during containment is not an error.
        rc, out, _ = run(admin_raw)
        assert rc == 0 and "already revoked" in out

        # 4. Unknown credential is NOT silent success — that would tell an operator
        #    the leak is contained when they are pointed at the wrong deployment.
        rc, _, err = run(new_caller_key())
        assert rc == 1
        assert "no such credential" in err
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})


@needs_db
def test_key_expiry_policy_and_last_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expiry is capped by policy, and a used key records that it was used.

    Expiry is easy to store and easy to leave unenforced, so this pins the
    policy surface rather than just the column.
    """
    from services.config import settings as cfg
    from services.db.session import org_session
    from services.governance import credentials

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"),
            {"n": f"Expiry-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()

    def expiry_of(key_hash: str) -> object:
        with admin.connect() as c:
            return c.execute(
                text("SELECT expires_at FROM api_key WHERE key_hash = :h"), {"h": key_hash}
            ).scalar_one()

    try:
        # No policy: keys are non-expiring, exactly as before this shipped. An
        # upgrade must not silently start expiring every existing caller's key.
        monkeypatch.setattr(cfg, "token_default_expiry_days", None)
        monkeypatch.setattr(cfg, "token_max_expiry_days", None)
        with org_session(oid) as s:
            raw = credentials.issue_key(s, credentials.create_caller(s, "nolimit"))
            s.commit()
        assert expiry_of(hash_token(raw)) is None

        # A cap binds even when the caller asks for longer — and caps rather than
        # refuses, so the operator gets a usable key instead of no key.
        monkeypatch.setattr(cfg, "token_max_expiry_days", 30)
        with org_session(oid) as s:
            capped = credentials.issue_key(
                s, credentials.create_caller(s, "greedy"), expires_in_days=3650
            )
            s.commit()
        exp = expiry_of(hash_token(capped))
        assert exp is not None
        from datetime import datetime, timedelta

        assert exp < datetime.now(UTC) + timedelta(days=31)

        # last_used_at starts empty and is stamped by a real verification — the
        # question "is this key dead weight" needs an answer before revoking.
        assert credentials.verify_caller_key(capped) is not None
        with admin.connect() as c:
            used = c.execute(
                text("SELECT last_used_at FROM api_key WHERE key_hash = :h"),
                {"h": hash_token(capped)},
            ).scalar_one()
        assert used is not None, "a verified key did not record last_used_at"
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
