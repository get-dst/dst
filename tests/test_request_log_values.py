"""A served answer always leaves its request-log row, whatever its values.

The row is written after the answer is computed. Warehouse values are not all
JSON: a Decimal, a date, a non-finite float. Serialising them used to happen
outside the logger's own error handling, so one such value raised out of the
logging step after the answer had computed; and jsonb refuses NaN, so a
non-finite value lost the row. Every value is now written as JSON can hold
it, and the logger never raises.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.app import app
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import fake_llm_providers
from services.contracts.lens_config import LensConfig
from services.contracts.trace import TraceLog
from services.db.session import org_session
from services.lenses import store
from services.lenses.store import LensBundle
from services.observability.logger import log_trace, trace_write_status
from tests.test_compiler import _capacity_model
from tests.test_query_api import _cleanup, _make_org_token, needs_db

client = TestClient(app, raise_server_exceptions=True)

SEPT = [
    {"field": "day", "op": ">=", "value": "2026-09-01"},
    {"field": "day", "op": "<=", "value": "2026-09-30"},
]


def _warehouse(tmp_path: Path) -> str:
    path = str(tmp_path / "clinics.duckdb")
    with duckdb.connect(path) as con:
        con.execute(
            "CREATE TABLE capacity AS SELECT * FROM (VALUES "
            "(DATE '2026-09-01', 'x', 70, 10), (DATE '2026-09-30', 'x', 70, 10), "
            "(DATE '2026-09-01', 'y', 35, 5), (DATE '2026-09-30', 'y', 35, 5)"
            ") t(day, clinic, slots_per_week, scheduled)"
        )
    return path


@needs_db
@pytest.mark.parametrize("log_samples", [False, True])
def test_a_period_weighted_ratio_is_served_and_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_samples: bool
) -> None:
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    path = _warehouse(tmp_path)
    monkeypatch.setattr(query_api, "_authorized_connector", lambda *_a, **_k: DuckDBConnector(path))
    org, raw = _make_org_token(settings.database_admin_url)
    config = LensConfig(name="ops", display_name="Ops", connections=["wh"])
    config.logging.log_samples = log_samples
    try:
        with org_session(org) as session:
            store.create_lens(session, LensBundle(config=config, semantic_model=_capacity_model()))
            store.publish(session, "ops")
        r = client.post(
            "/v1/lenses/ops/metrics",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "entity": "capacity",
                "metrics": ["utilisation"],
                "dimensions": ["clinic"],
                "filters": SEPT,
                "format": "structured",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        with create_engine(settings.database_admin_url).connect() as c:
            logged = c.execute(
                text("SELECT status FROM request_log WHERE request_id = :r"),
                {"r": body["request_id"]},
            ).scalar_one_or_none()
        assert logged == "ok"
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_values_json_cannot_spell_are_written_not_raised() -> None:
    org, _raw = _make_org_token(settings.database_admin_url)
    rid = "req_" + uuid.uuid4().hex[:16]
    before = trace_write_status()
    try:
        log_trace(
            TraceLog(
                request_id=rid,
                org_id=str(org),
                lens="ops",
                caller="analyst",
                question="utilisation in September",
                sample=[
                    [decimal.Decimal("0.1429"), datetime.date(2026, 9, 30)],
                    [float("nan"), float("inf")],
                ],
                latency_ms={"execute": float("nan")},
            )
        )
        assert trace_write_status() == before  # nothing lost
        with create_engine(settings.database_admin_url).connect() as c:
            sample, latency = c.execute(
                text("SELECT sample, latency FROM request_log WHERE request_id = :r"),
                {"r": rid},
            ).one()
        assert sample == [[0.1429, "2026-09-30"], [None, None]]
        assert latency == {"execute": None}
    finally:
        _cleanup(settings.database_admin_url, org)
