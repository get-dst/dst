"""Certifying a CORRECTED answer, and the way back out.

One review act used to produce three failures at once. An engineer filed a
correction with the right SQL, ruled it approved with ``--certify``, and got:

1. the ORIGINAL (rejected) SQL certified — the correction was never read;
2. a SECOND active pair for the same question, so which one served was decided
   by pgvector over identical embeddings;
3. no way back: files own only file-originated rows, so the review-promoted
   duplicate could only be destroyed (losing provenance) or left serving.

The wrong answer can then serve at ``confidence: verified · certified``.
These tests pin each half: the correction outranks the trace, one question
keeps one pair, and retiring stops serving while keeping the row.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.db.session import org_session
from services.lenses import connection_store

client = TestClient(app)


def _reachable(dsn: str) -> bool:
    try:
        create_engine(dsn).connect().close()
        return True
    except Exception:  # noqa: BLE001 — any connection failure means "skip"
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@pytest.fixture()
def org(monkeypatch):
    """A scratch org + admin token, torn down after each test.

    Mirrors tests/test_certified_provenance.py: the smart tier is pinned
    unresolvable so nothing dials an ambient provider key.
    """
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
        oid = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"),
            {"n": f"CertCorrT-{uuid4().hex[:8]}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        s.commit()
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "review",
            "request_log",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


QUESTION = "What was group consolidated revenue last month?"
WRONG_SQL = "SELECT sum(amount_eur) FROM consolidation"
RIGHT_SQL = "SELECT sum(amount_eur) FROM consolidation WHERE entity <> 'Elimination'"


def _seed_trace(oid: str, request_id: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, certification, "
                "latency, status) VALUES "
                "(:o, :r, 'customer_value', 'agent-1', :q, :sql, true, 1, "
                "'Group revenue was EUR 6,412,579.56.', 'revenue', 'high', 'none', "
                "'{}'::jsonb, 'ok')"
            ),
            {"o": oid, "r": request_id, "q": QUESTION, "sql": WRONG_SQL},
        )
    admin.dispose()


@needs_db
def test_ruled_correction_outranks_the_traced_sql(org, monkeypatch) -> None:
    """`correct` then `rule --certify` must store the CORRECTED SQL.

    Certifying the SQL a reviewer just declared wrong is the worst possible
    outcome of a correction: it serves verbatim from then on, wearing the
    `verified · certified` badge the reviewer's approval created.
    """
    oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    _seed_trace(oid, "req-corr-1")

    opened = client.post(
        "/v1/reviews",
        headers=headers,
        json={
            "request_id": "req-corr-1",
            "correction": {
                "kind": "number",
                "note": "elimination row double-counted",
                "corrected_sql": RIGHT_SQL,
            },
        },
    )
    assert opened.status_code in (200, 201), opened.text

    r = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-corr-1", headers=headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["corrected"] is True

    with org_session(oid) as s:
        stored = certify_store.get(s, r.json()["id"])
        assert stored is not None
        assert stored.sql == RIGHT_SQL, "the correction must win over the traced SQL"
        # Prose was composed from the WRONG SQL's rows — it cannot ride along.
        assert stored.verified_prose is None


@needs_db
def test_recertifying_a_question_replaces_it_instead_of_stacking(org, monkeypatch) -> None:
    """One question, one active pair — no arbitrary winner, nothing orphaned."""
    oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)

    _seed_trace(oid, "req-dup-1")
    first = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-dup-1", headers=headers
    )
    assert first.status_code == 201, first.text

    _seed_trace(oid, "req-dup-2")
    second = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-dup-2", headers=headers
    )
    assert second.status_code == 201, second.text
    assert second.json()["replaced"] == 1

    with org_session(oid) as s:
        same = [
            a
            for a in certify_store.list_for_lens(s, "customer_value")
            if a.question.strip().lower() == QUESTION.strip().lower()
        ]
        assert len(same) == 1, f"expected one pair for the question, got {len(same)}"
        assert same[0].id == second.json()["id"]


@needs_db
def test_retire_stops_serving_and_keeps_the_row(org, monkeypatch) -> None:
    """The missing verb: retired answers leave the serving set, not the record."""
    oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    _seed_trace(oid, "req-ret-1")
    created = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-ret-1", headers=headers
    )
    answer_id = created.json()["id"]

    r = client.post(f"/mgmt/lenses/customer_value/certified/{answer_id}/retire", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retired"

    with org_session(oid) as s:
        stored = certify_store.get(s, answer_id)
        assert stored is not None, "retire must not destroy the row (that is DELETE's job)"
        assert stored.status == "retired"
        assert not certify_store.is_active(stored.status)

    # Unknown ids 404 rather than silently succeeding.
    missing = client.post(
        "/mgmt/lenses/customer_value/certified/00000000-0000-0000-0000-000000000000/retire",
        headers=headers,
    )
    assert missing.status_code == 404
