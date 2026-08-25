"""The wrongness probe surfaced in the app.

POST /mgmt/connections/{name}/audit — query history → mine_drift →
execute_variants, end-to-end through a fake connector; connectors without a
history catalog are a clear 400. POST …/audit/draft-definition records a
ticket-less definition PatchCandidate that lands on the SAME approval rail as
corrections and distillation (GET /mgmt/lenses/{lens}/patches).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import Forbidden
from sqlalchemy import create_engine, text

from services import scheduler
from services.api import audit_store
from services.api.mgmt_audit import run_tracked
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.query_history import QueryRecord
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot
from services.db.session import org_session
from services.lenses import connection_store
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


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('AuditT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM review WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM patch_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM audit_run WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


# ─── the fake connector: history rows in, deterministic numbers out ───────────


def _rec(statement: str, runs: int, who: str, tool: str) -> QueryRecord:
    return QueryRecord(
        statement=statement,
        first_seen=datetime(2026, 5, 1, tzinfo=UTC),
        last_seen=datetime(2026, 6, 10, tzinfo=UTC),
        run_count=runs,
        principal=who,
        source_tool=tool,
    )


# One business metric, two conflicting definitions (different table + column).
HISTORY = [
    _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 25, "CFO", "Omni"),
    _rec(
        "SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22, "ANALYST", "Tableau"
    ),
    _rec("SELECT customer_name FROM crm.dim_customers LIMIT 10", 50, "OPS", "Excel"),  # no metric
]


class FakeHistoryConnector:
    """A duck-typed HistoryReader whose two revenue tables genuinely disagree."""

    kind = "duckdb"

    def __init__(self) -> None:
        self.history_days: list[int] = []
        self.executed: list[tuple[str, bool, int | None]] = []

    def introspect(self) -> SchemaSnapshot:
        return SchemaSnapshot(connection="wh", dialect=self.kind)

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        self.executed.append((sql, read_only, row_limit))
        value = 1000.0 if "fct_revenue_monthly" in sql else 1180.0
        return QueryResult(columns=["total"], rows=[[value]])

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        self.history_days.append(days)
        return HISTORY


class ForbiddenHistoryConnector(FakeHistoryConnector):
    """A history catalog the credentials can't read — BigQuery without jobs.listAll."""

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        self.history_days.append(days)
        raise Forbidden("Access Denied: project missing bigquery.jobs.listAll")


class NoHistoryConnector:
    """A read-capable connector with no query_history — the audit must refuse it."""

    kind = "mysql"

    def introspect(self) -> SchemaSnapshot:
        return SchemaSnapshot(connection="wh", dialect=self.kind)

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        return QueryResult()


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, connector: object) -> None:
    monkeypatch.setattr("services.api.mgmt_audit.resolve_connector", lambda name, org: connector)
    # Naming must not depend on the developer's local DeepSeek key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda ref: None)


# ─── the audit endpoint ───────────────────────────────────────────────────────


@needs_db
def test_audit_mines_history_into_executed_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    fake = FakeHistoryConnector()
    _patch_resolver(monkeypatch, fake)
    try:
        r = client.post("/mgmt/connections/wh/audit?days=7", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["records_scanned"] == 3
        assert fake.history_days == [7]  # the days param reaches the history catalog

        (finding,) = out["findings"]
        assert finding["severity"] == "conflict"
        assert finding["blast_radius"] == 47
        assert finding["metric_intent"] == "sum of revenue"  # llm=None → token-derived name
        # the variants carry the actual diverging numbers, most-run first
        assert [v["observed_rows"] for v in finding["variants"]] == [[[1000.0]], [[1180.0]]]
        assert [v["principals"] for v in finding["variants"]] == [["CFO"], ["ANALYST"]]
        # every execution went through the read-only, row-capped seam
        assert len(fake.executed) == 2
        assert all(read_only is True for _, read_only, _ in fake.executed)
        assert all(limit is not None for _, _, limit in fake.executed)
    finally:
        _cleanup(org)


@needs_db
def test_audit_execute_false_skips_the_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    fake = FakeHistoryConnector()
    _patch_resolver(monkeypatch, fake)
    try:
        r = client.post(
            "/mgmt/connections/wh/audit?execute=false", headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        (finding,) = r.json()["findings"]
        assert all(v["observed_rows"] is None for v in finding["variants"])
        assert fake.executed == []  # nothing ran against the warehouse
        assert fake.history_days == [30]  # default window
    finally:
        _cleanup(org)


@needs_db
def test_audit_refuses_connectors_without_history(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    _patch_resolver(monkeypatch, NoHistoryConnector())
    try:
        r = client.post("/mgmt/connections/wh/audit", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 400
        assert "query history" in r.json()["detail"]
    finally:
        _cleanup(org)


@needs_db
def test_audit_summary_reports_conflicts_coverage_and_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, FakeHistoryConnector())
    try:
        client.post("/mgmt/connections/wh/audit", headers=h)  # one revenue conflict
        s = client.get("/mgmt/connections/wh/audit/summary", headers=h)
        assert s.status_code == 200, s.text
        body = s.json()
        assert body["connection"] == "wh"
        assert body["has_run"] is True
        assert body["conflicts"] == 1
        # no lens governs the revenue metric yet → it is an ungoverned gap, no accuracy
        assert body["governed_metrics"] == 0
        assert body["ungoverned_metrics"] >= 1
        assert body["answer_accuracy"] is None
        assert body["accuracy_lenses"] == 0
        # per-metric governance: the revenue metric is ungoverned (no lens) yet
        assert body["metric_governance"].get("sum of revenue") is None
    finally:
        _cleanup(org)


@needs_db
def test_audit_unknown_connection_is_404() -> None:
    org, raw = _org_token()
    try:
        r = client.post("/mgmt/connections/nope/audit", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 404
    finally:
        _cleanup(org)


# ─── the connect→audit handoff status ─────────────────────────────────────────


@needs_db
def test_audit_status_never_run_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, FakeHistoryConnector())
    try:
        # Before any audit: the never-run state the empty connect screen reads.
        before = client.get("/mgmt/connections/wh/audit/status", headers=h)
        assert before.status_code == 200, before.text
        assert before.json()["state"] == "never_run"

        # The background audit, driven through its tracked lifecycle.
        with org_session(org) as session:
            run_tracked(session, "wh", days=7, org_id=org)

        status = client.get("/mgmt/connections/wh/audit/status", headers=h).json()
        assert status["state"] == "done"
        assert status["findings"] == 1
        assert status["conflicts"] == 1  # the headline the handoff shows
        assert status["created_at"] is not None

        # The completed run is now the standing result.
        latest = client.get("/mgmt/connections/wh/audit/latest", headers=h).json()
        assert latest["found"] is True
        assert len(latest["findings"]) == 1
    finally:
        _cleanup(org)


@needs_db
def test_audit_status_unsupported_is_not_an_infinite_spinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, NoHistoryConnector())
    try:
        # run_tracked must never raise — it records the terminal status instead.
        with org_session(org) as session:
            run_tracked(session, "wh", org_id=org)
        assert client.get("/mgmt/connections/wh/audit/status", headers=h).json()["state"] == (
            "unsupported"
        )
        # …and leaves no standing result to mistake for "still scanning".
        assert client.get("/mgmt/connections/wh/audit/latest", headers=h).json()["found"] is False
    finally:
        _cleanup(org)


@needs_db
def test_running_marker_does_not_blank_the_standing_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, FakeHistoryConnector())
    try:
        # A completed audit, then a fresh run starting (separate transactions so the
        # running marker sorts strictly after the completed row).
        with org_session(org) as session:
            run_tracked(session, "wh", org_id=org)
        with org_session(org) as session:
            audit_store.record_running(session, "wh", 30)
        # Status reflects the in-flight run…
        assert client.get("/mgmt/connections/wh/audit/status", headers=h).json()["state"] == (
            "running"
        )
        # …but the dashboard keeps showing the last completed result, not an empty row.
        latest = client.get("/mgmt/connections/wh/audit/latest", headers=h).json()
        assert latest["found"] is True
        assert len(latest["findings"]) == 1
    finally:
        _cleanup(org)


# ─── permission-denied history: degrade with a hint, never spam (UI findings) ─


@needs_db
def test_manual_audit_permission_denied_is_403_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    _patch_resolver(monkeypatch, ForbiddenHistoryConnector())
    try:
        r = client.post("/mgmt/connections/wh/audit", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 403
        assert "BigQuery Resource Viewer" in r.json()["detail"]
    finally:
        _cleanup(org)


@needs_db
def test_tracked_audit_records_permission_denied_as_unsupported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, ForbiddenHistoryConnector())
    try:
        with org_session(org) as session, caplog.at_level(logging.INFO, logger="dst"):
            run_tracked(session, "wh", org_id=org)  # must not raise
        state = client.get("/mgmt/connections/wh/audit/status", headers=h).json()["state"]
        assert state == "unsupported"
        # One actionable WARNING line, not an ERROR traceback.
        (rec,) = [r for r in caplog.records if "Resource Viewer" in r.getMessage()]
        assert rec.levelno == logging.WARNING
        assert rec.exc_info is None
    finally:
        _cleanup(org)


@needs_db
def test_scheduler_retries_permission_denied_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded unsupported run gates the staleness check: the next cycle skips."""
    org, _raw = _org_token()
    fake = ForbiddenHistoryConnector()
    _patch_resolver(monkeypatch, fake)
    try:
        with org_session(org) as session:
            connection_store.create_connection(session, "wh", "duckdb", {"path": ":memory:"}, None)
            session.commit()
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        scheduler._audit_org(org, cutoff)
        assert fake.history_days == [30]  # audited once, outcome recorded
        scheduler._audit_org(org, cutoff)
        assert fake.history_days == [30]  # fresh unsupported run → no re-audit this cycle
    finally:
        _cleanup(org)


@needs_db
def test_scheduler_keeps_org_context_across_per_connection_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SET LOCAL app.current_org dies with each per-connection commit, so a
    second connection's audit inserts org-less — RLS rejects it
    (InsufficientPrivilege on audit_run), no run is recorded, and the
    now-blind staleness check retries every cycle instead of once per
    interval. The scheduler re-sets the org context after each commit."""
    org, _raw = _org_token()
    _patch_resolver(monkeypatch, FakeHistoryConnector())
    try:
        with org_session(org) as session:
            connection_store.create_connection(session, "a", "duckdb", {"path": ":memory:"}, None)
            connection_store.create_connection(session, "b", "duckdb", {"path": ":memory:"}, None)
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        scheduler._audit_org(org, cutoff)
        with org_session(org) as session:
            rows = session.execute(
                text("SELECT DISTINCT connection FROM audit_run ORDER BY connection")
            ).all()
        # Both recorded, both visible under the org's RLS — connection "b" is the
        # one that used to vanish.
        assert [r[0] for r in rows] == ["a", "b"], rows
        scheduler._audit_org(org, cutoff)
        with org_session(org) as session:
            n = session.execute(text("SELECT count(*) FROM audit_run")).scalar()
        assert n == 2  # fresh runs gate the next cycle — no retry storm
    finally:
        _cleanup(org)


# ─── the audit can't lie ──────────────────────────────────────────────────────


@needs_db
def test_unsupported_connector_summary_omits_answer_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The F-35 defect: DuckDB has no query history, yet the summary served
    `answer_accuracy: 1.0` over 0 records — a vacuous pass wearing a KPI. The
    accuracy section now reports `unsupported` and the key is ABSENT, not null."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, NoHistoryConnector())
    try:
        with org_session(org) as session:
            run_tracked(session, "wh", org_id=org)  # records status=unsupported
        body = client.get("/mgmt/connections/wh/audit/summary", headers=h).json()
        assert body["accuracy_status"] == "unsupported"
        assert "answer_accuracy" not in body
    finally:
        _cleanup(org)


def _drift_report(connection: str = "wh") -> object:
    from services.governance import drift_watch
    from services.project import warehouse_drift as wd

    finding = wd.Finding(
        table="ops.orders",
        kind="column_dropped",
        detail="sales_channel",
        refs=[
            wd.LayerRef(kind="entity", name="orders", path="e.yaml", why="reads this table"),
            wd.LayerRef(kind="definition", name="channel", path="d.md", why="names it"),
            wd.LayerRef(kind="certified", name="Channel mix?", path="c.yaml", why="names it"),
        ],
    )
    deltas = ["ops.orders:column_dropped:sales_channel"]
    return drift_watch.DriftReport(
        connection=connection,
        fingerprint=drift_watch.fingerprint(connection, deltas),
        findings=[finding],
    )


@needs_db
def test_scheduled_drift_check_files_exactly_one_ticket_per_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaking drift → ONE review ticket (origin drift_audit) naming the
    affected entities/definitions/certified answers; the same drift on the next
    cycle attaches to it instead of filing a sibling — the dedup is the
    (org_id, fingerprint) unique index, not a check-before-insert race."""
    from services.governance import drift_watch

    org, _raw = _org_token()
    monkeypatch.setattr(drift_watch, "check_connection", lambda s, c, o: _drift_report())
    try:
        for _ in range(2):
            with org_session(org) as session:
                scheduler._drift_check(session, "wh", org)  # type: ignore[arg-type]
                session.commit()
        with org_session(org) as session:
            rows = session.execute(
                text("SELECT origin, state, correction, fingerprint FROM review")
            ).all()
        assert len(rows) == 1
        origin, state, correction, fingerprint = rows[0]
        assert origin == "drift_audit"
        assert state == "needs_human"
        assert fingerprint
        note = correction["note"]
        assert "definitions: channel" in note
        assert "certified answers: Channel mix?" in note
        assert "entities: orders" in note
    finally:
        _cleanup(org)


@needs_db
def test_scheduled_drift_check_files_nothing_on_non_breaking_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gained column is drift worth a look, not a breakage: filing a review
    for it would train reviewers to ignore drift findings."""
    from services.governance import drift_watch
    from services.project import warehouse_drift as wd

    org, _raw = _org_token()
    added = drift_watch.DriftReport(
        connection="wh",
        fingerprint="abc123",
        findings=[wd.Finding(table="ops.orders", kind="column_added", detail="x", refs=[])],
    )
    monkeypatch.setattr(drift_watch, "check_connection", lambda s, c, o: added)
    try:
        with org_session(org) as session:
            scheduler._drift_check(session, "wh", org)  # type: ignore[arg-type]
            session.commit()
        with org_session(org) as session:
            n = session.execute(text("SELECT count(*) FROM review")).scalar()
        assert n == 0
    finally:
        _cleanup(org)


# ─── the bridge: finding → definition patch on the shared approval rail ───────


@needs_db
def test_draft_definition_lands_on_the_lens_patch_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    _patch_resolver(monkeypatch, FakeHistoryConnector())
    try:
        bundle = jaffle_customer_value_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 201

        audit = client.post("/mgmt/connections/wh/audit", headers=h).json()
        (finding,) = audit["findings"]

        r = client.post(
            "/mgmt/connections/wh/audit/draft-definition",
            json={"finding": finding, "lens": "customer_value"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        cand = r.json()
        assert cand["kind"] == "definition"
        assert cand["status"] == "candidate"
        assert cand["ticket_id"] is None  # ticket-less, like distilled candidates
        assert cand["lens"] == "customer_value"
        assert cand["target"] == finding["metric_intent"]
        assert cand["diff_before"] is None  # term not yet defined on the lens
        # the proposal names the canon (most-agreed reading) and keeps the rejected ones
        assert "Canonical reading" in cand["diff_after"]
        assert "Rejected readings" in cand["diff_after"]
        assert "review before certifying" in cand["diff_after"]

        # THE point: it appears on the existing approval rail for this lens.
        listed = client.get("/mgmt/lenses/customer_value/patches", headers=h).json()
        assert [p["id"] for p in listed] == [cand["id"]]
        assert listed[0]["status"] == "candidate"
    finally:
        _cleanup(org)


@needs_db
def test_draft_definition_unknown_lens_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    fake = FakeHistoryConnector()
    _patch_resolver(monkeypatch, fake)
    try:
        audit = client.post("/mgmt/connections/wh/audit", headers=h).json()
        (finding,) = audit["findings"]
        r = client.post(
            "/mgmt/connections/wh/audit/draft-definition",
            json={"finding": finding, "lens": "nope"},
            headers=h,
        )
        assert r.status_code == 404
        # nothing recorded
        assert client.get("/mgmt/lenses/nope/patches", headers=h).json() == []
    finally:
        _cleanup(org)
