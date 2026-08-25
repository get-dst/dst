"""The corpus distiller — verified traces become candidate
patterns (certified / instruction / definition), never raw context.

The pure core (``distill_rows``) is exercised offline with ScriptedLLM; the
endpoint round-trip seeds ``request_log`` (the 0016 verification/certification
columns) and pins the ablation rule: ``context_chunk`` count never changes.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.evals.distill import TraceRow, cluster_traces, distill_rows, normalize_sql_shape
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm.registry import ResolvedModel

client = TestClient(app)

COUNT_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"
TOP_SQL = "SELECT customer_name FROM customers ORDER BY customer_lifetime_value DESC LIMIT {n}"

_NAME_JSON = '{"name": "top customers", "instruction": "Rank customers by lifetime value."}'
_NO_TERMS_JSON = '{"terms": []}'


# ─── clustering: literal variants share a shape (no embeddings) ───────────────


def test_normalize_strips_literals_and_formatting() -> None:
    a = normalize_sql_shape(COUNT_SQL, "duckdb")
    b = normalize_sql_shape("select count(*)   from customers where number_of_orders > 5", "duckdb")
    assert a is not None and a == b
    assert "?" in a and "1" not in a  # the literal is gone, not just rewritten
    assert normalize_sql_shape(TOP_SQL.format(n=3), "duckdb") != a
    assert normalize_sql_shape("SELECT 1 +", "duckdb") is None  # unparseable → skipped


def test_cluster_groups_literal_variants() -> None:
    rows = [
        TraceRow("top 3 customers?", TOP_SQL.format(n=3)),
        TraceRow("how many repeat customers?", COUNT_SQL),
        TraceRow("top 5 customers?", TOP_SQL.format(n=5)),
        TraceRow("top 10 customers?", TOP_SQL.format(n=10)),
        TraceRow("broken", "SELECT 1 +"),
    ]
    clusters = cluster_traces(rows, "duckdb")
    assert [len(c.rows) for c in clusters] == [3, 1]  # largest first; broken row dropped
    assert {r.question for r in clusters[0].rows} == {
        "top 3 customers?",
        "top 5 customers?",
        "top 10 customers?",
    }


# ─── distill_rows: certified / instruction / definition candidates ───────────


def test_exact_repeats_become_certified_candidate_without_llm() -> None:
    rows = [TraceRow("How many repeat customers?", COUNT_SQL)] * 3
    out = distill_rows(None, rows, jaffle_customer_value_bundle())
    assert len(out) == 1
    cand = out[0]
    assert cand.kind == "certified"
    assert cand.target == "How many repeat customers?"
    assert cand.diff_after == COUNT_SQL  # byte-identical SQL, not the stripped shape
    assert cand.status == "candidate"
    assert cand.ticket_id is None  # distilled candidates are ticket-less


def test_literal_variants_become_llm_named_instruction_candidate() -> None:
    llm = ScriptedLLM([_NAME_JSON, _NO_TERMS_JSON])  # one naming call, one terms call
    rows = [
        TraceRow("top 3 customers by lifetime value", TOP_SQL.format(n=3)),
        TraceRow("top 5 customers by lifetime value", TOP_SQL.format(n=5)),
        TraceRow("top 10 customers by lifetime value", TOP_SQL.format(n=10)),
    ]
    out = distill_rows(llm, rows, jaffle_customer_value_bundle())
    assert len(out) == 1
    cand = out[0]
    assert cand.kind == "instruction"
    assert cand.target == "customer_value"  # the instruction lands on the lens itself
    assert cand.diff_after.startswith("[top customers] Rank customers by lifetime value.")
    assert "LIMIT ?" in cand.diff_after  # the generalized shape, never a raw trace
    assert cand.status == "candidate" and cand.ticket_id is None


def test_instruction_candidate_falls_back_without_llm() -> None:
    rows = [
        TraceRow("top 3 customers", TOP_SQL.format(n=3)),
        TraceRow("top 5 customers", TOP_SQL.format(n=5)),
        TraceRow("top 9 customers", TOP_SQL.format(n=9)),
    ]
    out = distill_rows(None, rows, jaffle_customer_value_bundle())
    assert len(out) == 1
    assert out[0].kind == "instruction"
    assert "Recurring pattern (3 verified traces)" in out[0].diff_after


def test_below_min_count_distills_nothing() -> None:
    rows = [TraceRow("q", COUNT_SQL), TraceRow("q", COUNT_SQL)]
    assert distill_rows(None, rows, jaffle_customer_value_bundle()) == []
    assert distill_rows(None, rows, jaffle_customer_value_bundle(), min_count=2) != []


def test_undefined_terms_become_definition_candidates_with_hallucination_guard() -> None:
    terms = (
        '{"terms": ['
        '{"term": "churn rate", "body": "Share of customers with no order in 90 days."},'
        '{"term": "wombat factor", "body": "Not in any question."},'
        '{"term": "repeat customer", "body": "Already defined on the lens."}]}'
    )
    llm = ScriptedLLM([_NAME_JSON, terms])
    rows = [
        TraceRow("what is the churn rate for repeat customer cohorts?", TOP_SQL.format(n=3)),
        TraceRow("churn rate by month", TOP_SQL.format(n=5)),
        TraceRow("churn rate last quarter", TOP_SQL.format(n=9)),
    ]
    out = distill_rows(llm, rows, jaffle_customer_value_bundle())
    defs = [c for c in out if c.kind == "definition"]
    assert [c.target for c in defs] == ["churn rate"]  # hallucinated + defined terms dropped
    assert defs[0].diff_after == "Share of customers with no order in 90 days."
    assert defs[0].diff_before is None and defs[0].status == "candidate"


# ─── endpoint round-trip (needs Postgres) ─────────────────────────────────────


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


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('DistillT') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_request(
    org: object,
    question: str,
    sql: str,
    *,
    confidence: str = "verified",
    certification: str = "none",
    status: str = "ok",
) -> None:
    """A request_log row with the 0016 verification/certification columns
    (pattern from tests/test_observe_drill.py::_seed_request)."""
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, confidence, verification, certification, "
                "latency, status) "
                "VALUES (:o, :r, 'customer_value', 'agent-1', :q, :sql, true, 1, 'ans', "
                ":conf, CAST(:ver AS jsonb), :cert, '{}'::jsonb, :status)"
            ),
            {
                "o": org,
                "r": f"req-{uuid.uuid4()}",
                "q": question,
                "sql": sql,
                "conf": confidence,
                "cert": certification,
                "status": status,
                "ver": f'{{"grade": "{confidence}", "checks": []}}',
            },
        )


def _context_chunk_count(org: object) -> tuple[int, int]:
    """(org-scoped, global) context_chunk counts — the ablation-rule probe."""
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        mine = c.execute(
            text("SELECT count(*) FROM context_chunk WHERE org_id = :o"), {"o": org}
        ).scalar_one()
        total = c.execute(text("SELECT count(*) FROM context_chunk")).scalar_one()
    return int(mine), int(total)


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM patch_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM context_chunk WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_distill_endpoint_persists_candidates_and_never_touches_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeded verified traces → POST distill → candidates in the patch store;
    excluded traces (partial / certified-served / errored) contribute nothing;
    re-running is idempotent; context_chunk count is byte-for-byte unchanged."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    # A fresh ScriptedLLM per assist_llm() call → re-runs replay the same script.
    monkeypatch.setattr(
        "services.api.mgmt_distill.assist_llm",
        lambda: ResolvedModel(ScriptedLLM([_NAME_JSON, _NO_TERMS_JSON]), "fake", "scripted"),
    )
    try:
        body = jaffle_customer_value_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=body, headers=h).status_code == 201

        # 3 exact repeats → certified candidate.
        for _ in range(3):
            _seed_request(org, "how many repeat customers?", COUNT_SQL)
        # 3 literal variants of one shape → instruction candidate.
        for n in (3, 5, 10):
            _seed_request(org, f"top {n} customers by lifetime value", TOP_SQL.format(n=n))
        # Disqualified traces (one per 0016 gate) — their shape must produce nothing.
        bad = "SELECT count(*) FROM orders WHERE status = 'returned'"
        _seed_request(org, "returned orders?", bad, confidence="partial")
        _seed_request(org, "returned orders?", bad, certification="certified")
        _seed_request(org, "returned orders?", bad, status="error")

        chunks_before = _context_chunk_count(org)

        r = client.post("/mgmt/lenses/customer_value/distill", headers=h)
        assert r.status_code == 201, r.text
        cands = r.json()
        by_kind = {c["kind"]: c for c in cands}
        assert set(by_kind) == {"certified", "instruction"}
        assert by_kind["certified"]["target"] == "how many repeat customers?"
        assert by_kind["certified"]["diff_after"] == COUNT_SQL
        assert by_kind["instruction"]["target"] == "customer_value"
        assert "LIMIT ?" in by_kind["instruction"]["diff_after"]
        assert all(c["status"] == "candidate" and c["ticket_id"] is None for c in cands)
        # The disqualified trio (same question, repeated 3×) was never mined.
        assert all("FROM orders" not in c["diff_after"] for c in cands)
        assert all(c["target"] != "returned orders?" for c in cands)

        # One approval surface: the candidates are on the patches list.
        listed = client.get("/mgmt/lenses/customer_value/patches", headers=h).json()
        assert {p["id"] for p in listed} == {c["id"] for c in cands}

        # Re-run is idempotent — already-proposed patterns are not re-recorded.
        again = client.post("/mgmt/lenses/customer_value/distill", headers=h)
        assert again.status_code == 201 and again.json() == []
        assert len(client.get("/mgmt/lenses/customer_value/patches", headers=h).json()) == 2

        # The ablation rule: raw traces are NEVER injected into the context store.
        assert _context_chunk_count(org) == chunks_before
    finally:
        _cleanup(org)


@needs_db
def test_distill_unknown_lens_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    monkeypatch.setattr("services.api.mgmt_distill.assist_llm", lambda: None)
    try:
        r = client.post("/mgmt/lenses/nope/distill", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 404
    finally:
        _cleanup(org)
