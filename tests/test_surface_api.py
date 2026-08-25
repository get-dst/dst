"""Surface area = the router-lens's accept-vs-decline rate.

The pure rollup (rate + trend + decline clustering) is unit-tested offline; the
GET /mgmt/surface endpoint + the rate/clusters from seeded routing_decision rows
run under needs_db. The clustering turns a count of missed questions into named
uncovered-metric groups — the gap candidates.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.route_store import RoutingDecision
from services.api.surface import cluster_declines, compute_surface
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.db.session import org_session

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


def _d(question: str, covered: bool) -> RoutingDecision:
    return RoutingDecision(
        id="x",
        question=question,
        routed_lens="finance" if covered else None,
        score=0.9 if covered else 0.3,
        covered=covered,
        created_at=datetime(2026, 6, 16),
    )


# ─── the pure rollup (offline) ────────────────────────────────────────────────


def test_clusters_group_declines_by_uncovered_subject() -> None:
    declines = [
        _d("how many customers churned last month?", False),
        _d("what is our churn rate?", False),
        _d("show churn by segment", False),
        _d("how many support tickets are open?", False),
        _d("average support ticket resolution time?", False),
    ]
    clusters = cluster_declines(declines)
    labels = {c.label for c in clusters}
    assert "churn" in labels and "support" in labels
    by_label = {c.label: c for c in clusters}
    assert by_label["churn"].count == 3  # the three churn questions land together
    assert by_label["support"].count == 2
    # largest cluster first; examples are verbatim questions
    assert clusters[0].label == "churn"
    assert any("churn" in ex.lower() for ex in by_label["churn"].examples)


def test_surface_rate_and_trend_are_the_accept_vs_decline_rate() -> None:
    # 3 routed of 4 asked → surface 0.75
    decisions = [
        _d("revenue this quarter?", True),
        _d("net invoiced?", True),
        _d("outstanding receivables?", True),
        _d("how many tickets are open?", False),
    ]
    routed = sum(1 for x in decisions if x.covered)
    rate = routed / len(decisions)
    assert rate == 0.75


# ─── the endpoint + DB rollup ─────────────────────────────────────────────────


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('SurfaceT') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _seed(org: object, rows: list[tuple[str, str | None, float, bool]]) -> None:
    with org_session(org) as session:
        for question, lens, score, covered in rows:
            session.execute(
                text(
                    "INSERT INTO routing_decision (org_id, question, routed_lens, score, covered) "
                    "VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid, "
                    ":q, :l, :s, :c)"
                ),
                {"q": question, "l": lens, "s": score, "c": covered},
            )


@needs_db
def test_compute_surface_from_seeded_decisions() -> None:
    org, _ = _org_token()
    try:
        _seed(
            org,
            [
                ("revenue this quarter?", "finance", 0.9, True),
                ("net invoiced?", "finance", 0.88, True),
                ("outstanding receivables?", "finance", 0.85, True),
                ("how many customers churned?", None, 0.3, False),
                ("what is our churn rate?", None, 0.28, False),
                ("how many support tickets are open?", None, 0.25, False),
            ],
        )
        with org_session(org) as session:
            report = compute_surface(session, days=30)
        assert report.asked == 6
        assert report.routed == 3
        assert report.declined == 3
        assert report.surface_area == 0.5
        labels = {c.label for c in report.uncovered_clusters}
        assert "churn" in labels and "support" in labels
    finally:
        _cleanup(org)


@needs_db
def test_surface_endpoint_returns_rate_and_clusters() -> None:
    org, raw = _org_token()
    try:
        _seed(
            org,
            [
                ("revenue this quarter?", "finance", 0.9, True),
                ("how many customers churned?", None, 0.3, False),
                ("what is our churn rate?", None, 0.28, False),
            ],
        )
        r = client.get("/mgmt/surface", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["asked"] == 3 and body["routed"] == 1 and body["declined"] == 2
        assert abs(body["surface_area"] - (1 / 3)) < 1e-6
        churn = next(c for c in body["uncovered_clusters"] if c["label"] == "churn")
        assert churn["count"] == 2
    finally:
        _cleanup(org)


@needs_db
def test_surface_empty_org_is_a_clean_zero_not_a_crash() -> None:
    org, raw = _org_token()
    try:
        r = client.get("/mgmt/surface", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        body = r.json()
        assert body["asked"] == 0
        assert body["surface_area"] is None  # no rate to report
        assert body["uncovered_clusters"] == []
    finally:
        _cleanup(org)


@needs_db
def test_degraded_declines_count_apart_never_as_coverage_loss() -> None:
    """A decider outage must read as an outage — degraded declines are
    excluded from asked/declined/surface_area and the clusters, counted apart."""
    from services.api import route_store

    org, _ = _org_token()
    try:
        _seed(
            org,
            [
                ("revenue this quarter?", "finance", 0.9, True),
                ("how many customers churned?", None, 0.3, False),
            ],
        )
        with org_session(org) as session:
            route_store.record(
                session, "board numbers?", None, 0.86, False, degraded="DECIDER_DOWN"
            )
        with org_session(org) as session:
            report = compute_surface(session, days=30)
        assert report.degraded_declines == 1
        assert report.asked == 2 and report.routed == 1 and report.declined == 1
        assert report.surface_area == 0.5  # the outage did not drag the rate down
        assert all("board" not in c.label for c in report.uncovered_clusters)
    finally:
        _cleanup(org)
