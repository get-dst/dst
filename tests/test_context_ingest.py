"""File/text extraction, text ingestion, Context Layer API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.context import store as ctx_store
from services.context.extract import UnsupportedFile, extract_text
from services.context.ingest import ingest_text
from services.contracts.fakes import HashEmbedder
from services.db.session import org_session
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def test_extract_text_plain() -> None:
    assert extract_text("notes.md", b"# Hello\nworld") == "# Hello\nworld"
    assert "a,b" in extract_text("data.csv", b"a,b\n1,2")


def test_extract_rejects_unknown() -> None:
    with pytest.raises(UnsupportedFile):
        extract_text("image.png", b"\x89PNG")


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
        org = c.execute(text("INSERT INTO org (name) VALUES ('CtxTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM context_chunk WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_ingest_text_and_list_sources() -> None:
    org, _ = _make_org_token()
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())
            res = ingest_text(
                s,
                "customer_value",
                "note:churn",
                "Churn means no order in 90 days. " * 50,
                HashEmbedder(),
            )
            assert res["chunks"] >= 1
            sources = ctx_store.list_sources(s, "customer_value")
            assert any(src["source"] == "note:churn" for src in sources)
            assert sources[0]["last_indexed"] is not None
    finally:
        _cleanup(org)


@needs_db
def test_context_api_upload_list_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: HashEmbedder())
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())

        up = client.post(
            "/mgmt/lenses/customer_value/context/upload",
            headers=h,
            files={"file": ("playbook.md", b"# Playbook\nDefine active users.", "text/markdown")},
        )
        assert up.status_code == 200, up.text
        assert up.json()["chunks"] >= 1

        txt = client.post(
            "/mgmt/lenses/customer_value/context/text",
            headers=h,
            json={"source": "note:manual", "text": "Repeat customers have 2+ orders."},
        )
        assert txt.status_code == 200, txt.text

        listing = client.get("/mgmt/lenses/customer_value/context", headers=h)
        assert listing.status_code == 200
        names = {s["source"] for s in listing.json()["sources"]}
        assert "upload:playbook.md" in names and "note:manual" in names

        d = client.delete("/mgmt/lenses/customer_value/context/note:manual", headers=h)
        assert d.status_code == 204
    finally:
        _cleanup(org)


@needs_db
def test_upload_rejects_unsupported_type(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: HashEmbedder())
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())
        r = client.post(
            "/mgmt/lenses/customer_value/context/upload",
            headers=h,
            files={"file": ("logo.png", b"\x89PNG", "image/png")},
        )
        assert r.status_code == 400
    finally:
        _cleanup(org)


@needs_db
def test_upload_over_cap_is_413(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: HashEmbedder())
    monkeypatch.setattr("services.config.settings.max_upload_mb", 1)
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())
        r = client.post(
            "/mgmt/lenses/customer_value/context/upload",
            headers=h,
            files={"file": ("big.md", b"x" * (1024 * 1024 + 1), "text/markdown")},
        )
        assert r.status_code == 413
        assert "DST_MAX_UPLOAD_MB" in r.json()["detail"]
    finally:
        _cleanup(org)
