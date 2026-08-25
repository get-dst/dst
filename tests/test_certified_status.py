"""Certified_answer.status — active | retired.

Retirement is explicit and keeps history: a retired answer still lists (mgmt)
and exports (certified_answers.yaml renders the key only when retired — active
files stay byte-identical), but it is never served, never matched, and never
nagged for re-verify. The file key round-trips through apply's upsert; an
ABSENT key is not an edit (an old file must never silently un-retire).
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.certify.store import CertifiedAnswer
from services.config import settings
from services.contracts.fakes import HashEmbedder
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.llm import registry as llm_registry
from services.semantic.files import render_semantic_files

client = TestClient(app)

# Captured before any fixture pins it — tests that fake providers and need the
# REAL resolution ladder restore this.
_REAL_RESOLVE = llm_registry.resolve


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


@pytest.fixture()
def org(monkeypatch):
    from services.project import apply as apply_engine

    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    # The certify self-test is UNCONDITIONAL now — pin the smart tier to
    # unresolvable so applies degrade to the loud skip instead of dialing an
    # ambient provider key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertStatusT') RETURNING id")
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
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"


def _files(answers: list[dict[str, object]]) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    # eval_gate: warn — this file tests status round-trips, and retiring the
    # last active answer under the block default trips the starvation guard
    # (which has its own tests in test_certified_gate_rewire).
    cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
    cfg["eval_gate"] = "warn"
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    return files


def _apply(headers: dict[str, str], files: dict[str, str]) -> list[dict[str, object]]:
    return client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()


# ── rendering (pure) ─────────────────────────────────────────────────────────


def test_status_renders_only_when_retired() -> None:
    """Active answers must stay byte-identical to pre-status files."""
    active = CertifiedAnswer("a1", "l", _Q, _SQL, "alex", "2026-07-30T00:00:00")
    retired = CertifiedAnswer(
        "a2", "l", "Old question?", _SQL, "alex", "2026-07-30T00:00:00", status="retired"
    )
    tree = render_lens_repo(jaffle_customer_value_bundle(), certified_answers=[active, retired])
    pairs = yaml.safe_load(tree["certified_answers.yaml"])
    assert "status" not in pairs[0]
    assert pairs[1]["status"] == "retired"


# ── round-trip + never-served (scratch DB) ───────────────────────────────────


@needs_db
def test_status_round_trips_and_absent_key_never_unretires(org) -> None:
    oid, headers = org
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "active"

    # Retire via the file key; the row keeps its history.
    _apply(headers, _files([{"question": _Q, "sql": _SQL, "status": "retired"}]))
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.status == "retired" and stored.sql == _SQL

    # An export carries the key — and only for the retired answer.
    out = client.get("/mgmt/project/export", headers=headers).json()
    pairs = yaml.safe_load(out["files"]["lenses/customer_value/certified_answers.yaml"])
    assert pairs[0]["status"] == "retired"

    # Re-applying a file WITHOUT the key is not an edit: stays retired.
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "retired"

    # Explicit re-certification: status: active brings it back.
    _apply(headers, _files([{"question": _Q, "sql": _SQL, "status": "active"}]))
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "active"


@needs_db
def test_bad_status_value_is_rejected_by_name(org) -> None:
    """A typo'd status must not silently default to active (un-retiring)."""
    _oid, headers = org
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL, "status": "retire"}]))
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any(
        f"certified answer '{_Q}' rejected: status must be 'active' or 'retired'" in e
        for e in row["errors"]
    )
    assert out[-1]["action"] == "aborted"


@needs_db
def test_retired_is_never_matched_served_or_nagged(org, monkeypatch) -> None:
    from services.contracts.fakes import ScriptedLLM, fake_llm_providers

    oid, headers = org
    answers = [
        {"question": _Q, "sql": _SQL, "status": "retired"},
        {"question": "How many orders are there?", "sql": "SELECT count(*) FROM orders"},
    ]
    _apply(headers, _files(answers))
    with org_session(oid) as s:
        by_q = {a.question: a for a in certify_store.list_for_lens(s, "customer_value")}
        retired_id, active_id = by_q[_Q].id, by_q["How many orders are there?"].id
        # search (the serve/match path) sees only the active answer — even on the
        # retired question's own embedding.
        hits = certify_store.search(s, "customer_value", HashEmbedder().embed([_Q])[0])
        assert [h.answer.id for h in hits] == [active_id]

    # The caller-facing library advertises active only; mgmt lists both (history).
    lib = client.get("/v1/lenses/customer_value/certified", headers=headers).json()
    assert [e["cert_id"] for e in lib["certified"]] == [active_id]
    mgmt = client.get("/mgmt/lenses/customer_value/certified", headers=headers).json()
    assert {e["id"]: e["status"] for e in mgmt} == {
        retired_id: "retired",
        active_id: "active",
    }

    # The deterministic door 404s a retired answer; the active one still serves.
    # (the org fixture pinned resolve to None for the applies above — this part
    # needs the real ladder over the faked providers)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr("services.llm.registry.resolve", _REAL_RESOLVE)
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM(["There are some orders."]),
    )
    r = client.post(f"/v1/lenses/customer_value/certified/{retired_id}/run", headers=headers)
    assert r.status_code == 404
    r = client.post(f"/v1/lenses/customer_value/certified/{active_id}/run", headers=headers)
    assert r.status_code == 200 and r.json()["certification"] == "certified"

    # A shared edit that flags the retired answer's bindings nags nobody: the
    # only customers-bound answer is retired — history, not open re-verify debt.
    edited = _files(answers)
    edited["semantic/entities/customers.yaml"] = edited["semantic/entities/customers.yaml"].replace(
        "One row per customer.", "One row per customer, always."
    )
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert "certified" not in row


@needs_db
def test_deleting_an_entry_deletes_it_files_win(org) -> None:
    """The worst authoring friction, final doctrine: a file-deleted
    entry no longer keeps serving behind a warning — files win, the entry
    DELETES on apply with the loud count. (The interim orphan warning is gone
    with it; retiring in place remains the keep-history alternative.)"""
    oid, headers = org
    second = "how many customers ordered at least once?"
    second_sql = "SELECT count(*) AS n FROM customers WHERE number_of_orders >= 1"
    _apply(
        headers,
        _files([{"question": _Q, "sql": _SQL}, {"question": second, "sql": second_sql}]),
    )
    # Delete the second entry from the file: green apply, the answer is GONE.
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert not row["errors"]
    assert "certified answers: created 0, updated 0, deleted 1, unchanged 1" in row["applied"]
    assert not any("absence never deletes" in w for w in row["warnings"])
    with org_session(oid) as s:
        stored = {a.question: a.status for a in certify_store.list_for_lens(s, "customer_value")}
    assert second not in stored and stored[_Q] == "active"
