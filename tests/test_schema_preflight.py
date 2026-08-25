"""A database behind this build must be LOUD — it was the quietest failure here.

The failure: `dst serve` starts against a schema two revisions behind
(nothing in `services/` read `alembic_version`), `/ready` answers
`{"status":"ready","db":"ok"}` because its check was `SELECT 1`, and every answer was
served correctly, with a `request_id`, at exit 0 — while every `request_log` INSERT
failed inside a FastAPI background task where no caller could see it. The receipt for
an answer that was given did not exist, and `dst correct` then blamed the
request_id. Everything below is one of those four surfaces refusing to be quiet.
"""

from __future__ import annotations

import argparse
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.reviews import _untraced_detail
from services.app import app
from services.cli import main as cli
from services.config import settings
from services.contracts.trace import TraceLog
from services.db import schema_state as schema
from services.observability import logger as trace_logger

client = TestClient(app)


def _revisions() -> tuple[str, str]:
    """(head, its parent) from THIS build's scripts — never hard-coded, so the file
    survives the next migration."""
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(schema.alembic_config())
    head = scripts.get_current_head()
    assert head is not None
    return head, scripts.get_revision(head).down_revision  # type: ignore[return-value]


def _stamp(version: str) -> None:
    """Rewrite alembic_version only — no DDL. The schema itself stays at head, so a
    restore is a second UPDATE and no other test can observe a half-migrated database."""
    engine = create_engine(settings.database_admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": version})
    finally:
        engine.dispose()


# ── the read itself, against the real alembic_version ─────────────────────────


@pytest.mark.needs_db
def test_schema_state_is_ok_at_head() -> None:
    head, _ = _revisions()
    state = schema.schema_state()
    assert (state.status, state.current, state.head) == ("ok", head, head)
    assert state.summary() == f"ok ({head})"


def test_schema_state_sees_a_behind_database() -> None:
    head, parent = _revisions()
    try:
        _stamp(parent)
        state = schema.schema_state()
    finally:
        _stamp(head)
    assert state.status == "behind"
    assert state.pending == (head,)  # exactly the one revision that was not applied
    assert head in state.summary() and parent in state.summary()


def test_a_revision_from_the_future_is_ahead_not_behind() -> None:
    """Older code on a newer schema is the SAFE deploy order (expand, then roll the
    code) and works end to end. Refusing it would break the safe path."""
    head, _ = _revisions()
    try:
        _stamp("9999_from_a_newer_build")
        state = schema.schema_state()
    finally:
        _stamp(head)
    assert state.status == "ahead"


# ── serve refuses, and refuses ONLY on `behind` ───────────────────────────────


def _serve_args() -> argparse.Namespace:
    return argparse.Namespace(host="127.0.0.1", port=0, reload=False)


def test_serve_refuses_a_behind_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    behind = schema.SchemaState("behind", "0035", "0038", ("0038", "0037", "0036"))
    monkeypatch.setattr(schema, "schema_state", lambda: behind)
    assert cli._serve(_serve_args()) == 1
    err = capsys.readouterr().err
    assert "schema is behind" in err
    assert "0035" in err and "0038" in err  # where it is, where it must be
    assert "audit trail" in err and "dst migrate" in err  # the cost and the fix


@pytest.mark.parametrize(
    "state",
    [
        schema.SchemaState("ok", "0038", "0038"),
        schema.SchemaState("ahead", "9999", "0038"),
        schema.SchemaState("unknown", detail="cannot read alembic_version (timeout)"),
    ],
)
def test_serve_starts_on_every_state_that_is_not_behind(
    state: schema.SchemaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unknown` must not refuse either: a database still coming up is not a broken
    one, and a least-privilege deployment may not be able to answer the question."""
    import uvicorn

    started: list[str] = []
    monkeypatch.setattr(schema, "schema_state", lambda: state)
    monkeypatch.setattr(cli, "_announce_ready", lambda url: None)
    monkeypatch.setattr(settings, "web_dist", "already-set")
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: started.append(target))
    assert cli._serve(_serve_args()) == 0
    assert started == ["services.app:app"]


# ── /ready reports the schema instead of SELECT 1 ─────────────────────────────


@pytest.mark.needs_db
def test_ready_reports_the_schema_at_head(live_client: TestClient) -> None:
    head, _ = _revisions()
    body = live_client.get("/ready").json()
    assert body["status"] == "ready"
    assert body["schema"] == f"ok ({head})"
    assert body["traces"] == "ok"


@pytest.mark.needs_db
def test_ready_degrades_on_a_behind_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact body /ready used to return — `{"status":"ready","db":"ok"}` — on a schema
    two revisions behind. `db` stays "ok" (the database IS reachable); the lie was
    calling the install ready."""
    monkeypatch.setattr(
        schema, "schema_state", lambda: schema.SchemaState("behind", "0035", "0038")
    )
    body = client.get("/ready").json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["schema"].startswith("BEHIND") and "0035" in body["schema"]


# ── a failed trace write is counted and surfaced, never swallowed ─────────────


def _trace() -> TraceLog:
    return TraceLog(
        request_id="req_lost",
        org_id="not-a-uuid",  # the INSERT cannot succeed with this org_id
        lens="customer_value",
        caller="admin",
        question="How many customers are repeat customers?",
    )


def test_a_failed_trace_write_is_named_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It must not raise (Starlette abandons the background tasks queued behind it)
    and it must not be silent (that is the whole bug)."""
    monkeypatch.setattr(trace_logger, "_FAILED_WRITES", 0)
    monkeypatch.setattr(trace_logger, "_LAST_ERROR", "")
    with caplog.at_level(logging.CRITICAL, logger="dst"):
        trace_logger.log_trace(_trace())  # returns; does not raise
    assert "AUDIT TRACE LOST for req_lost" in caplog.text
    assert trace_logger._FAILED_WRITES == 1
    assert trace_logger.trace_write_status().startswith("LOSING WRITES")


def test_ready_degrades_while_traces_are_being_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_logger, "_FAILED_WRITES", 3)
    monkeypatch.setattr(trace_logger, "_LAST_ERROR", 'column "generator_tier" does not exist')
    body = client.get("/ready").json()
    assert body["status"] == "degraded"
    assert body["traces"].startswith("LOSING WRITES — 3")


# ── the message that used to blame the request_id ─────────────────────────────


def test_untraced_request_names_the_schema_when_that_is_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema, "schema_state", lambda: schema.SchemaState("behind", "0035", "0038")
    )
    detail = _untraced_detail("req_10a615a0e00f4a65")
    assert "schema is behind" in detail and "dst migrate" in detail
    assert "The request_id is fine" in detail


def test_untraced_request_stays_terse_when_the_schema_is_fine() -> None:
    assert _untraced_detail("req_x") == "no traced request 'req_x' to review"


# ── migrate says what it did ──────────────────────────────────────────────────


def test_migrate_reports_what_it_applied() -> None:
    """`migrated to head` printed identically after two revisions and after none."""
    behind = schema.SchemaState("behind", "0035", "0038", ("0038", "0037", "0036"))
    assert cli._migrate_result(behind) == (
        "migrated 0035 → 0038 — 3 revisions applied (0036, 0037, 0038)"
    )
    one = schema.SchemaState("behind", "0037", "0038", ("0038",))
    assert cli._migrate_result(one) == "migrated 0037 → 0038 — 1 revision applied (0038)"
    assert cli._migrate_result(schema.SchemaState("ok", "0038", "0038")) == (
        "already at head (0038) — nothing to apply"
    )
    fresh = schema.SchemaState("behind", None, "0038", detail="no alembic_version table")
    assert cli._migrate_result(fresh) == "schema created at 0038"
