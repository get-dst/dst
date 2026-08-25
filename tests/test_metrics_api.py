"""The structured metrics door: POST /v1/lenses/{name}/metrics.

Names in, rows out, no model call anywhere on the path. Same authorization,
sql_guard, row caps and trace as every other door — the only thing that changes
is who writes the SQL.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.query import describe_intent
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.query_intent import IntentFilter, IntentOrder, QueryIntent
from services.db.session import org_session
from services.lenses import store
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


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Metrics') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for table in ("request_log", "certified_answer", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _seed(org: object) -> None:
    with org_session(org) as session:
        if not store.lens_exists(session, "customer_value"):
            store.create_lens(session, jaffle_customer_value_bundle())
            store.publish(session, "customer_value")


def _post(raw: str, body: dict) -> object:
    return client.post(
        "/v1/lenses/customer_value/metrics",
        json=body,
        headers={"Authorization": f"Bearer {raw}"},
    )


# ── the intent rendering that lands in the request log ───────────────────────


def test_describe_intent_spells_the_whole_intent() -> None:
    """The trace logs the caller's question on every other door; this one has no
    question, and "(structured intent)" would blind the audit log exactly where it
    is most auditable."""
    text_ = describe_intent(
        QueryIntent(
            metrics=["revenue"],
            dimensions=["region"],
            filters=[IntentFilter(field="status", op="=", value="won")],
            order_by=[IntentOrder(field="revenue", dir="desc")],
            limit=5,
        )
    )
    assert text_ == "revenue by region where status = won ordered by revenue desc limit 5"


def test_describe_intent_of_a_bare_listing() -> None:
    assert describe_intent(QueryIntent(dimensions=["region"])) == "rows by region"


# ── the door ─────────────────────────────────────────────────────────────────


@needs_db
def test_metrics_door_returns_rows_with_no_model_call() -> None:
    """No LLM is stubbed anywhere in this test: if the door reached for one, it
    would fail. That is the point of the door."""
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(raw, {"metrics": ["total_clv"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sql"] and "SUM(" in body["sql"].upper()
        assert body["data"]["rows"], body
        # structured by default: the pipeline's deterministic placeholder, not prose
        assert "no prose was composed" in body["answer"]
    finally:
        _cleanup(org)


@needs_db
def test_metrics_door_groups_and_orders() -> None:
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(
            raw,
            {
                "metrics": ["total_clv"],
                "dimensions": ["customer_name"],
                "order_by": [{"field": "total_clv", "dir": "desc"}],
                "limit": 3,
            },
        )
        assert r.status_code == 200, r.text
        rows = r.json()["data"]["rows"]
        assert len(rows) == 3
        values = [row[1] for row in rows]
        assert values == sorted(values, reverse=True)
    finally:
        _cleanup(org)


@needs_db
def test_metrics_door_joins_to_another_entity() -> None:
    """The B1 win, reachable without a model: `revenue` lives on orders,
    `customer_name` on customers, and the declared join carries it."""
    org, raw = _make_org_token()
    _seed(org)
    try:
        body = {"entity": "orders", "metrics": ["revenue"], "dimensions": ["customer_name"]}
        r = _post(raw, body)
        assert r.status_code == 200, r.text
        assert "JOIN" in r.json()["sql"].upper()
        assert r.json()["data"]["rows"]
    finally:
        _cleanup(org)


@needs_db
def test_an_unknown_metric_is_a_400_that_names_it() -> None:
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(raw, {"metrics": ["not_a_metric"]})
        assert r.status_code == 400
        assert "not_a_metric" in r.json()["detail"]
    finally:
        _cleanup(org)


@needs_db
def test_an_empty_intent_is_refused_rather_than_scanning_the_table() -> None:
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(raw, {})
        assert r.status_code == 400
        assert "neither a metric nor a dimension" in r.json()["detail"]
    finally:
        _cleanup(org)


@needs_db
def test_the_door_needs_a_caller_like_every_other_door() -> None:
    r = client.post("/v1/lenses/customer_value/metrics", json={"metrics": ["total_clv"]})
    assert r.status_code in (401, 403)


@needs_db
def test_an_unknown_lens_is_404() -> None:
    org, raw = _make_org_token()
    try:
        r = client.post(
            "/v1/lenses/ghost/metrics",
            json={"metrics": ["x"]},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_the_metrics_door_buckets_a_trend_by_grain() -> None:
    """`grain` is the whole of "by month": the caller never names a date expression,
    and the compiled path cannot group on the raw timestamp."""
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(raw, {"entity": "orders", "metrics": ["revenue"], "grain": "month"})
        assert r.status_code == 200, r.text
        assert "DATE_TRUNC" in r.json()["sql"].upper()
        rows = r.json()["data"]["rows"]
        assert rows and len(rows) < 12  # months, not one row per order
    finally:
        _cleanup(org)


@needs_db
def test_grain_without_a_time_field_is_a_400_naming_the_fix() -> None:
    org, raw = _make_org_token()
    _seed(org)
    try:
        r = _post(raw, {"entity": "customers", "metrics": ["total_clv"], "grain": "month"})
        assert r.status_code == 400
        assert "default_time_field" in r.json()["detail"]
    finally:
        _cleanup(org)
