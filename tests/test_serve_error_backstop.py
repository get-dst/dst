"""The first binder error is detected and filed, not the eighty-first.

The failure shape: a warehouse rename breaks a staple question, the caller gets
raw Binder shrapnel, the reviewer sees nothing, and detection waits on a
business user noticing. The backstop pinned here: a
warehouse execution error on a governed query files ONE deduplicated review
ticket, the caller-facing error is a deterministic template that names the
ticket and NEVER the raw engine text, a confirmed drift marks the lens
degraded until an apply clears it, and a certified answer that errors serves
`certified_failed` — never plain `certified` beside an error.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import FakeConnector, fake_llm_providers
from services.contracts.response import QueryResponse
from services.contracts.trace import TraceLog
from services.db.session import org_session
from services.governance import drift_watch
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value, jaffle_customer_value_bundle
from services.project import warehouse_drift as wd
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import PipelineResult, run_query

client = TestClient(app)

RAW_ERROR = 'Binder Error: Referenced column "sales_channel" not found in FROM clause!'


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


# ── the pipeline half: error_kind + certified_failed (no DB) ─────────────────


class FailingConnector(FakeConnector):
    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None) -> object:
        raise RuntimeError(RAW_ERROR)


def _run(certification: str) -> PipelineResult:
    return run_query(
        question="How many repeat customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FailingConnector(),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=None,
        certification=certification,  # type: ignore[arg-type]
    )


def test_warehouse_execution_error_is_tagged_for_the_backstop() -> None:
    res = _run("none")
    assert res.response.status == "error"
    assert res.error_kind == "warehouse"
    assert res.trace.error is not None and "Binder Error" in res.trace.error


def test_certified_execution_error_serves_certified_failed() -> None:
    """`certification: "certified"` beside `status: "error"` is the badge
    outranking the fault — the one state the trust layer must never emit."""
    res = _run("certified")
    assert res.response.status == "error"
    assert res.response.certification == "certified_failed"
    assert res.trace.certification == "certified_failed"


def test_ok_certified_serves_keep_the_plain_badge() -> None:
    res = run_query(
        question="How many repeat customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=None,
        certification="certified",
    )
    assert res.response.status == "ok"
    assert res.response.certification == "certified"
    assert res.error_kind is None


def test_cost_cap_rejection_is_a_config_class_never_drift() -> None:
    """A bytesBilledLimitExceeded rejection classified into a
    drift bucket told the user a column was missing and filed a false
    data-team ticket — a dst limit wearing another team's vocabulary. The
    config class matches FIRST and names the fix."""
    err = (
        "500 Query exceeded limit for bytes billed: 1000000000. "
        "5894045696 or higher required.; reason: bytesBilledLimitExceeded"
    )
    cause = drift_watch.cause_class(err)
    assert cause == drift_watch.CONFIG_LIMIT_CAUSE
    assert "DST_BIGQUERY_MAX_BYTES_BILLED" in cause
    assert "column" not in cause and "table" not in cause
    # The cap has a reviewed home in dst.yaml, and the remediation must reach
    # for it BEFORE the env var: offering only the export taught a real project
    # to carry the value in a single operator's shell instead of the file.
    assert "dst.yaml" in cause and "max_bytes_billed" in cause
    assert cause.index("dst.yaml") < cause.index("DST_BIGQUERY_MAX_BYTES_BILLED")


def test_genuine_missing_column_still_classifies_as_drift() -> None:
    # REGRESSION: the drift path keeps working for actual drift.
    assert (
        drift_watch.cause_class("Unrecognized name: renamed_col; column not found")
        == "a column the query needs is missing or renamed"
    )


def test_cause_class_is_deterministic_and_never_the_raw_text() -> None:
    assert drift_watch.cause_class(RAW_ERROR) == "a column the query needs is missing or renamed"
    assert (
        drift_watch.cause_class("Catalog Error: Table with name orders does not exist")
        == "a table the query needs is missing or renamed"
    )
    assert drift_watch.cause_class("something inscrutable") == "query execution failure"


# ── the API half: template, ticket dedup, degraded until clear ───────────────


def _org_and_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('ServeErr') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_lens(org: object) -> None:
    with org_session(org) as session:
        lens_store.create_lens(session, jaffle_customer_value_bundle())
        lens_store.publish(session, "customer_value")


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for table in ("review", "request_log", "lens", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> None:
    """Pin run_query's outcome; the wiring under test starts where it returns."""
    monkeypatch.setattr(settings, "providers", fake_llm_providers())

    def fake_run_query(**kw: object) -> PipelineResult:
        rid = "req_" + uuid.uuid4().hex[:16]
        if fail:
            return PipelineResult(
                response=QueryResponse(
                    lens=str(kw["lens_name"]),
                    status="error",
                    answer=f"The query could not be executed: {RAW_ERROR}",
                    sql="SELECT 1",
                    request_id=rid,
                ),
                trace=TraceLog(
                    request_id=rid,
                    org_id=str(kw["org_id"]),
                    lens=str(kw["lens_name"]),
                    caller=str(kw["caller"]),
                    question=str(kw["question"]),
                    sql="SELECT 1",
                    valid=False,
                    status="error",
                    error=RAW_ERROR,
                ),
                error_kind="warehouse",
            )
        return PipelineResult(
            response=QueryResponse(
                lens=str(kw["lens_name"]), answer="42.", sql="SELECT 1", request_id=rid
            ),
            trace=TraceLog(
                request_id=rid,
                org_id=str(kw["org_id"]),
                lens=str(kw["lens_name"]),
                caller=str(kw["caller"]),
                question=str(kw["question"]),
                sql="SELECT 1",
                valid=True,
                status="ok",
            ),
        )

    monkeypatch.setattr(query_api, "run_query", fake_run_query)


def _query(raw: str) -> dict[str, object]:
    r = client.post(
        "/v1/lenses/customer_value/query",
        json={"q": "how many repeat customers?"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _tickets(org: object) -> list[tuple[str, str, str]]:
    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c:
        rows = c.execute(
            text("SELECT ticket_id, origin, fingerprint FROM review WHERE org_id = :o"),
            {"o": org},
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


@needs_db
def test_serve_error_is_templated_ticketed_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template names the cause class and the ticket; the raw engine text
    stays in the trace. A second identical failure ATTACHES to the same ticket."""
    org, raw = _org_and_token()
    _seed_lens(org)
    _fake_pipeline(monkeypatch, fail=True)
    try:
        body = _query(raw)
        assert body["status"] == "error"
        assert "Binder Error" not in str(body)  # no raw engine text on the wire
        assert "sales_channel" not in str(body)
        tickets = _tickets(org)
        assert len(tickets) == 1
        ticket_id, origin, fingerprint = tickets[0]
        assert origin == "serve_error"
        assert fingerprint
        assert (
            body["answer"] == "This question hit a data problem (a column the query needs "
            f"is missing or renamed) — ticket {ticket_id} opened for the data team."
        )

        # Second identical failure: same fingerprint, same ticket, still one row.
        again = _query(raw)
        assert _tickets(org) == tickets
        assert ticket_id in str(again["answer"])
    finally:
        _cleanup(org)


@needs_db
def test_confirmed_drift_marks_the_lens_degraded_until_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift behind the error ⇒ the degraded mark rides EVERY subsequent answer
    (not just failing ones) until a successful apply clears it."""
    org, raw = _org_and_token()
    _seed_lens(org)
    report = drift_watch.DriftReport(
        connection="jaffle",
        fingerprint=drift_watch.fingerprint("jaffle", ["customers:column_dropped:sales_channel"]),
        findings=[
            wd.Finding(
                table="customers",
                kind="column_dropped",
                detail="sales_channel",
                refs=[wd.LayerRef(kind="entity", name="customers", path="e.yaml", why="reads it")],
            )
        ],
    )
    monkeypatch.setattr(drift_watch, "check_connection", lambda s, c, o: report)
    try:
        _fake_pipeline(monkeypatch, fail=True)
        body = _query(raw)
        assert any("DEGRADED: schema drift" in d for d in body["degraded"])
        (ticket,) = _tickets(org)
        assert ticket[1] == "serve_error"
        assert ticket[0] in str(body["degraded"])  # the mark names the ticket

        # A healthy answer on the SAME lens still carries the standing mark.
        _fake_pipeline(monkeypatch, fail=False)
        ok_body = _query(raw)
        assert ok_body["status"] == "ok"
        assert any("DEGRADED: schema drift" in d for d in ok_body["degraded"])

        # The successful-apply act clears it; the next answer drops the mark
        # (other degradations — here the no-embedder note — are their own facts).
        with org_session(org) as session:
            assert lens_store.clear_degraded(session) == 1
        cleared = _query(raw)
        assert not any("DEGRADED: schema drift" in d for d in cleared["degraded"])
    finally:
        _cleanup(org)


def test_a_dialect_function_error_classifies_as_a_generation_fault() -> None:
    """'Function not found: YEAR' is dst's own defect, not the data team's."""
    assert (
        drift_watch.cause_class("400 Function not found: YEAR at [4:7]")
        == drift_watch.GENERATION_FAULT_CAUSE
    )
    assert (
        drift_watch.cause_class(
            "No matching signature for function DATE_DIFF for argument types: STRING, DATE"
        )
        == drift_watch.GENERATION_FAULT_CAUSE
    )
    # …and the data-side classes keep their lane, in both directions.
    assert "table" in drift_watch.cause_class("Table not found: proj.marts.gone")


@needs_db
def test_a_generation_fault_opens_no_data_team_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop templates an honest dst-defect answer and the
    review queue stays empty — a data engineer cannot action 'the mart is
    fine, the query is wrong'."""
    org, raw = _org_and_token()
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())

    def fake_run_query(**kw: object) -> PipelineResult:
        rid = "req_" + uuid.uuid4().hex[:16]
        err = "400 Function not found: YEAR at [4:7]"
        return PipelineResult(
            response=QueryResponse(
                lens=str(kw["lens_name"]),
                status="error",
                answer=f"The query could not be executed: {err}",
                sql="SELECT 1",
                request_id=rid,
            ),
            trace=TraceLog(
                request_id=rid,
                org_id=str(kw["org_id"]),
                lens=str(kw["lens_name"]),
                caller=str(kw["caller"]),
                question=str(kw["question"]),
                sql="SELECT 1",
                valid=False,
                status="error",
                error=err,
            ),
            error_kind="warehouse",
        )

    monkeypatch.setattr(query_api, "run_query", fake_run_query)
    try:
        body = _query(raw)
        assert body["status"] == "error"
        assert "dst defect" in str(body["answer"])
        assert "not a data problem" in str(body["answer"])
        assert "hit a data problem" not in str(body["answer"])  # the old misattribution
        assert "no data-team ticket" in str(body["answer"])
        assert _tickets(org) == []
    finally:
        _cleanup(org)
