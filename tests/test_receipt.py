"""Answer receipts: signed at serve, checkable later by anyone in the org.

The last unverifiable hop: a number pasted out of its session carried nothing a
skeptic could check. The receipt signs the serve's claims (HMAC over canonical
JSON, DST_SECRET_KEY, first-signs-all-verify rotation) and /v1/verify-receipt
re-checks them two ways — signature AND field-by-field against the logged
trace. Unsigned and unkeyed are named states, never silent passes and never
reported as forgery.
"""

from __future__ import annotations

import socket
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.runtime import receipt

client = TestClient(app)


def _reachable(url: str) -> bool:
    from sqlalchemy.engine import make_url

    u = make_url(url)
    try:
        with socket.create_connection((u.host or "localhost", u.port or 5432), timeout=0.5):
            return True
    except OSError:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


def _build(mp: pytest.MonkeyPatch, key: str = "k1") -> receipt.Receipt:
    mp.setattr(settings, "secret_key", key)
    return receipt.build(
        request_id="req_r1",
        lens="customer_value",
        certification="none",
        cert_id=None,
        confidence="verified",
        sql="SELECT 1",
        data_as_of="2026-08-11",
    )


def test_signed_receipt_verifies_and_tamper_breaks_it(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _build(monkeypatch)
    assert r.digest and receipt.check_signature(r) == "valid"
    assert receipt.check_signature(r.model_copy(update={"lens": "other"})) == "invalid"
    assert receipt.check_signature(r.model_copy(update={"confidence": "partial"})) == "invalid"


def test_key_rotation_still_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _build(monkeypatch, key="old")
    monkeypatch.setattr(settings, "secret_key", "new,old")
    assert receipt.check_signature(r) == "valid"
    monkeypatch.setattr(settings, "secret_key", "new")
    assert receipt.check_signature(r) == "invalid"


def test_unsigned_and_unkeyed_are_named_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", None)
    unsigned = receipt.build(
        request_id="r",
        lens="l",
        certification="none",
        cert_id=None,
        confidence=None,
        sql=None,
        data_as_of=None,
    )
    assert unsigned.digest is None and receipt.check_signature(unsigned) == "unsigned"
    signed = _build(monkeypatch)
    monkeypatch.setattr(settings, "secret_key", None)
    # A digest this server holds no key for is a config gap, not forgery.
    assert receipt.check_signature(signed) == "unkeyed"


# ─── the verify door, against a real trace ───────────────────────────────────


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('RcptTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_trace(org: object, request_id: str, sql: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, confidence, certification, latency, status) VALUES "
                "(:o, :r, 'customer_value', 'agent-1', 'how many?', :s, true, 1, '19', "
                "'verified', 'none', '{}'::jsonb, 'ok')"
            ),
            {"o": org, "r": request_id, "s": sql},
        )


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_verify_door_cross_checks_the_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    rid = f"req_{uuid.uuid4().hex[:12]}"
    _seed_trace(org, rid, "SELECT 1")
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "secret_key", "k1")
    try:
        r = receipt.build(
            request_id=rid,
            lens="customer_value",
            certification="none",
            cert_id=None,
            confidence="verified",
            sql="SELECT 1",
            data_as_of=None,
        )
        good = client.post("/v1/verify-receipt", headers=h, json=r.model_dump()).json()
        assert good["ok"] is True and good["signature"] == "valid"
        assert good["trace_found"] is True and good["mismatches"] == []
        assert good["question"] == "how many?"

        # A doctored receipt fails BOTH ways: the signature broke and the trace
        # disagrees — and both are said, field by field.
        forged = r.model_copy(update={"lens": "finance", "confidence": "partial"})
        bad = client.post("/v1/verify-receipt", headers=h, json=forged.model_dump()).json()
        assert bad["ok"] is False and bad["signature"] == "invalid"
        assert set(bad["mismatches"]) == {"lens", "confidence"}

        # No trace: nothing to vouch for, said plainly.
        ghost = r.model_copy(update={"request_id": "req_none"})
        gone = client.post("/v1/verify-receipt", headers=h, json=ghost.model_dump()).json()
        assert gone["ok"] is False and gone["trace_found"] is False
    finally:
        _cleanup(org)
