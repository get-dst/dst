"""Certified-answer provenance (source / verified_by) end to end.

Pure halves pin the yaml contract — the two keys render only when set (old files
stay byte-identical; derived bindings never render) and round-trip through the
loader. DB halves drive the apply upsert (source survives a sql edit) and the
review-promotion stamp (source="review:<request_id>" unless the caller supplies one).
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
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.project.loader import load_lens_source
from services.semantic.files import render_semantic_files

client = TestClient(app)

_SOURCE = "looker:dashboards/42 'MRR by segment'"
_ANSWER = CertifiedAnswer(
    id="ca_1",
    lens="customer_value",
    question="How many repeat customers are there?",
    sql="SELECT COUNT(*) FROM customers WHERE number_of_orders > 1",
    created_by="alex",
    created_at="2026-07-01T00:00:00Z",
)


# ── render ↔ load (pure) ─────────────────────────────────────────────────────


def test_yaml_round_trips_source_and_verified_by() -> None:
    stamped = CertifiedAnswer(
        **{**_ANSWER.__dict__, "source": _SOURCE, "verified_by": "exec KPI dashboard"}
    )
    files = render_lens_repo(jaffle_customer_value_bundle(), certified_answers=[stamped])
    pairs = yaml.safe_load(files["certified_answers.yaml"])
    assert pairs[0]["source"] == _SOURCE
    assert pairs[0]["verified_by"] == "exec KPI dashboard"
    loaded = load_lens_source(files)
    assert loaded.certified_answers[0]["source"] == _SOURCE
    assert loaded.certified_answers[0]["verified_by"] == "exec KPI dashboard"


def test_yaml_without_provenance_is_byte_identical() -> None:
    """Old files must not change by a byte: unset keys never render, and derived
    bindings are DB-only even when set on the row."""
    unstamped = CertifiedAnswer(**{**_ANSWER.__dict__, "bindings": {"entity/customers": "h1"}})
    files = render_lens_repo(jaffle_customer_value_bundle(), certified_answers=[unstamped])
    legacy = yaml.safe_dump(  # the exact pre-CB1 render
        [
            {
                "question": _ANSWER.question,
                "sql": _ANSWER.sql,
                "created_by": _ANSWER.created_by,
                "created_at": _ANSWER.created_at,
            }
        ],
        sort_keys=False,
        allow_unicode=True,
    )
    assert files["certified_answers.yaml"] == legacy
    assert "bindings" not in files["certified_answers.yaml"]


# ── the DB halves ────────────────────────────────────────────────────────────


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
    # The certify self-test is UNCONDITIONAL now — pin the smart tier to
    # unresolvable so applies degrade to the loud skip instead of dialing an
    # ambient provider key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertProvT') RETURNING id")
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
            "request_log",
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


def _project_files(answers: list[dict[str, object]]) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    return files


@needs_db
def test_apply_stamps_and_preserves_source(org, monkeypatch) -> None:
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files(
        [
            {
                "question": _ANSWER.question,
                "sql": _ANSWER.sql,
                "source": _SOURCE,
                "verified_by": "exec KPI dashboard",
            }
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.source == _SOURCE
        assert stored.verified_by == "exec KPI dashboard"

    # A sql edit in a file that DROPS the provenance keys still preserves them.
    files = _project_files([{"question": _ANSWER.question, "sql": _ANSWER.sql + " -- edited"}])
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 1, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.sql.endswith("-- edited")
        assert stored.source == _SOURCE
        assert stored.verified_by == "exec KPI dashboard"

    # An explicit source edit in the file lands (round-trip, files win).
    files = _project_files(
        [{"question": _ANSWER.question, "sql": _ANSWER.sql + " -- edited", "source": "kpi-doc"}]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 1, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].source == "kpi-doc"


def _seed_trace(oid: object, request_id: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, certification, "
                "latency, status) VALUES "
                "(:o, :r, 'customer_value', 'agent-1', 'how many repeat customers?', "
                "'SELECT count(*) FROM customers WHERE number_of_orders > 1', true, 1, "
                "'There are 19 repeat customers.', 'repeat_customer', 'high', 'none', "
                "'{}'::jsonb, 'ok')"
            ),
            {"o": oid, "r": request_id},
        )
    admin.dispose()


@needs_db
def test_certify_from_request_stamps_review_source(org, monkeypatch) -> None:
    oid, headers = org
    _seed_trace(oid, "req-cb1")
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    r = client.post("/mgmt/lenses/customer_value/certified/from-request/req-cb1", headers=headers)
    assert r.status_code == 201, r.text
    with org_session(oid) as s:
        stored = certify_store.get(s, r.json()["id"])
        assert stored is not None and stored.source == "review:req-cb1"

    # A caller-supplied source wins over the stamp.
    _seed_trace(oid, "req-cb1b")
    r = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-cb1b",
        headers=headers,
        json={"source": "metabase:card/7 'Repeat customers'", "verified_by": "maija"},
    )
    assert r.status_code == 201, r.text
    with org_session(oid) as s:
        stored = certify_store.get(s, r.json()["id"])
        assert stored is not None
        assert stored.source == "metabase:card/7 'Repeat customers'"
        assert stored.verified_by == "maija"
