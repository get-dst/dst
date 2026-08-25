"""One gap, three views — router ↔ audit ↔ lens scope.

The capstone binding. The pure binder (de-dupe + metric-governance resolution) is
unit-tested offline; the GET /mgmt/gap-map and GET /mgmt/lenses/{name}/coverage
endpoints run under needs_db over seeded routing_decision + audit_run rows and
published lenses.

The load-bearing assertions:
  - the gap-map decomposes drifting/uncovered metrics into governed-by-lens-X vs
    UNGOVERNED, de-duped across the router's misses and the Audit's findings;
  - a finding binds to a lens by METRIC GOVERNANCE (the metric matches a governed
    metric) — NOT raw table overlap (the revenue finding's gold table is named by
    no lens, yet it still binds to finance, which governs the revenue *metric*);
  - the per-lens in-scope view shows ONLY the governed slice (finance sees
    revenue/overdue, never churn/win-rate) — out-of-scope drift excluded by
    construction.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import audit_store
from services.api.surface import UncoveredCluster
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import LensConfig, ModelConfig
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.db.session import org_session
from services.lenses import store as lens_store
from services.lenses.store import LensBundle
from services.probe.drift import DriftFinding, DriftVariant
from services.probe.gap_map import (
    _governed_metrics,
    build_gap_map,
    governing_lens,
    lens_in_scope_findings,
)
from services.router.profiles import coverage_profile

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

CONNECTION = "wh"


# ─── fixture lenses: finance governs revenue/overdue; sales governs win-rate ───


def _finance_bundle() -> LensBundle:
    """A finance lens. Its entities are sourced from BRONZE invoices, but the revenue
    DRIFT lives in a gold mart — proving the binding is by metric, not table."""
    sm = SemanticModel(
        lens="finance",
        dialect="snowflake",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection=CONNECTION, table="bronze.invoices"),
                fields=[
                    Field(name="invoice_id", type="integer"),
                    Field(name="net_invoiced_revenue", type="number"),
                ],
                metrics=[
                    Metric(name="net_invoiced_revenue", agg="sum", expr="net_invoiced_revenue"),
                ],
            )
        ],
        definitions=[
            Definition(term="overdue_invoices", body="Invoices past their due date."),
        ],
    )
    cfg = LensConfig(
        name="finance",
        display_name="Finance",
        description="Revenue and receivables.",
        connections=[CONNECTION],
        model=ModelConfig(model="claude-sonnet-4-6"),
    )
    return LensBundle(config=cfg, semantic_model=sm)


def _sales_bundle() -> LensBundle:
    sm = SemanticModel(
        lens="sales",
        dialect="snowflake",
        entities=[
            Entity(
                name="opportunities",
                source=EntitySource(connection=CONNECTION, table="gold.opportunities"),
                fields=[Field(name="opp_id", type="integer")],
                metrics=[Metric(name="win_rate", agg="avg", expr="is_won")],
            )
        ],
        definitions=[Definition(term="pipeline", body="Open opportunities by stage.")],
    )
    cfg = LensConfig(
        name="sales",
        display_name="Sales",
        description="Pipeline and win-rate.",
        connections=[CONNECTION],
        model=ModelConfig(model="claude-sonnet-4-6"),
    )
    return LensBundle(config=cfg, semantic_model=sm)


# ─── audit findings + router decline clusters (fixtures) ──────────────────────


def _finding(metric: str, tables: list[str], runs: int) -> DriftFinding:
    return DriftFinding(
        metric_intent=metric,
        variants=[
            DriftVariant(
                statement=f"SELECT SUM(x) FROM {tables[0]}",
                source_tables=tables,
                run_count=runs,
                distinguishing="d",
            )
        ],
        blast_radius=runs,
        severity="conflict",
    )


# Revenue drift runs over a GOLD mart no lens names in scope — must still bind to
# finance by the revenue *metric*. Churn is governed by no lens (an ungoverned gap).
REVENUE_FINDING = _finding("sum of revenue", ["gold.fct_revenue_monthly"], 10)
CHURN_FINDING = _finding("count of churn", ["gold.churn_monthly"], 5)

SUPPORT_DECLINE = UncoveredCluster(
    label="support", count=4, examples=["how many support tickets are open?"]
)
CHURN_DECLINE = UncoveredCluster(label="churn", count=3, examples=["what is our churn rate?"])


# ─── the pure binder (offline) ────────────────────────────────────────────────


def test_governance_binds_by_metric_not_table_overlap() -> None:
    """The revenue finding's table (gold.fct_revenue_monthly) is named by NO lens —
    finance is sourced from bronze.invoices. Yet the revenue *metric* binds to
    finance: governance is over the metric's identity, not its storage."""
    finance = coverage_profile(_finance_bundle())
    assert "gold.fct_revenue_monthly" not in finance.scope  # table is NOT in finance scope
    assert "bronze.invoices" in finance.scope
    sales = coverage_profile(_sales_bundle())
    governed = sorted(
        [("finance", _governed_metrics(finance)), ("sales", _governed_metrics(sales))]
    )
    assert governing_lens("sum of revenue", governed) == "finance"  # by metric, not table
    assert governing_lens("count of churn", governed) is None  # ungoverned


def test_gap_map_decomposes_governed_vs_ungoverned() -> None:
    profiles = [coverage_profile(_finance_bundle()), coverage_profile(_sales_bundle())]
    gm = build_gap_map(
        CONNECTION,
        findings=[REVENUE_FINDING, CHURN_FINDING],
        declines=[SUPPORT_DECLINE, CHURN_DECLINE],
        profiles=profiles,
    )
    by_metric = {g.governed_by: g for g in gm.governed}
    # revenue is governed by finance (the audit finding bound by metric governance)
    assert "finance" in by_metric
    assert "revenue" in by_metric["finance"].metric.lower()
    assert by_metric["finance"].sources == ["audit"]

    # churn + support are UNGOVERNED gaps
    ungoverned_labels = {g.metric for g in gm.ungoverned}
    assert any("churn" in m.lower() for m in ungoverned_labels)
    assert any("support" in m.lower() for m in ungoverned_labels)
    assert gm.governed_count == 1
    assert gm.ungoverned_count == 2  # churn (merged) + support


def test_gap_map_dedupes_churn_across_both_sources() -> None:
    """The audit's 'count of churn' and the router's 'churn' decline are ONE gap,
    evidenced by both sources — de-duped by metric, not double-counted."""
    gm = build_gap_map(
        CONNECTION,
        findings=[CHURN_FINDING],
        declines=[CHURN_DECLINE],
        profiles=[coverage_profile(_finance_bundle())],
    )
    churn = [g for g in gm.gaps if "churn" in g.metric.lower()]
    assert len(churn) == 1  # one merged gap, not two
    assert churn[0].sources == ["audit", "router"]
    assert churn[0].governed_by is None
    assert churn[0].blast_radius == 5 and churn[0].decline_count == 3


def test_finance_in_scope_view_excludes_out_of_scope_drift() -> None:
    """The finance lens's in-scope view shows ONLY revenue/overdue findings — never
    churn (ungoverned) or win-rate (sales' scope). Out-of-scope drift is excluded by
    construction: a lens is never shown another scope's failure as its own."""
    finance = coverage_profile(_finance_bundle())
    overdue = _finding("count of overdue invoices", ["gold.ar_aging"], 7)
    win_rate = _finding("avg of win rate", ["gold.opportunities"], 9)
    cov = lens_in_scope_findings(
        finance,
        findings=[REVENUE_FINDING, CHURN_FINDING, overdue, win_rate],
        declines=[CHURN_DECLINE, SUPPORT_DECLINE],
    )
    metrics = {f.metric.lower() for f in cov.in_scope}
    assert any("revenue" in m for m in metrics)
    assert any("overdue" in m for m in metrics)
    # NEVER the out-of-scope ones
    assert not any("churn" in m for m in metrics)
    assert not any("win" in m for m in metrics)
    assert not any("support" in m for m in metrics)
    assert all(f.governed for f in cov.in_scope)  # the lens claims each (accountable)


def test_sales_in_scope_view_claims_only_its_metric() -> None:
    sales = coverage_profile(_sales_bundle())
    win_rate = _finding("avg of win rate", ["gold.opportunities"], 9)
    cov = lens_in_scope_findings(
        sales,
        findings=[REVENUE_FINDING, win_rate],
        declines=[CHURN_DECLINE],
    )
    metrics = {f.metric.lower() for f in cov.in_scope}
    assert metrics == {"avg of win rate"}  # only win-rate; not revenue, not churn


# ─── the endpoints + DB rollup ────────────────────────────────────────────────


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('GapT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM audit_run WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _seed(org: object) -> None:
    """Publish the two lenses, record an audit run (revenue governed + churn
    ungoverned), and seed router declines clustering to churn + support."""
    with org_session(org) as session:
        for bundle in (_finance_bundle(), _sales_bundle()):
            lens_store.create_lens(session, bundle)
            lens_store.publish(session, bundle.config.name)
        # audit run for the connection: a governed revenue conflict + an ungoverned churn
        audit_store.record_run(session, CONNECTION, 30, 100, [REVENUE_FINDING, CHURN_FINDING])
        # router declines (live misses): churn ×2, support ×1 — all uncovered
        for q in (
            "how many customers churned?",
            "what is our churn rate?",
            "how many support tickets are open?",
        ):
            session.execute(
                text(
                    "INSERT INTO routing_decision (org_id, question, routed_lens, score, covered) "
                    "VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid, "
                    ":q, NULL, 0.3, false)"
                ),
                {"q": q},
            )


@needs_db
def test_gap_map_endpoint_decomposes_warehouse() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        _seed(org)
        r = client.get(f"/mgmt/gap-map?connection={CONNECTION}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        gaps = body["gaps"]
        governed = [g for g in gaps if g["governed_by"] is not None]
        ungoverned = [g for g in gaps if g["governed_by"] is None]

        # revenue governed by finance (metric governance — its gold table is in no lens)
        rev = next(g for g in governed if "revenue" in g["metric"].lower())
        assert rev["governed_by"] == "finance"
        assert "audit" in rev["sources"]

        # churn + support are ungoverned gaps; churn merges audit + router evidence
        labels = {g["metric"].lower() for g in ungoverned}
        assert any("churn" in m for m in labels)
        assert any("support" in m for m in labels)
        churn = next(g for g in ungoverned if "churn" in g["metric"].lower())
        assert set(churn["sources"]) == {"audit", "router"}
    finally:
        _cleanup(org)


@needs_db
def test_lens_coverage_endpoint_shows_only_in_scope() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        _seed(org)
        r = client.get(f"/mgmt/lenses/finance/coverage?connection={CONNECTION}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        metrics = {f["metric"].lower() for f in body["in_scope"]}
        assert any("revenue" in m for m in metrics)  # in scope
        assert not any("churn" in m for m in metrics)  # ungoverned — excluded
        assert not any("win" in m for m in metrics)  # sales' scope — excluded
        assert all(f["governed"] for f in body["in_scope"])
    finally:
        _cleanup(org)


@needs_db
def test_lens_coverage_unknown_lens_is_404() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.get(f"/mgmt/lenses/nope/coverage?connection={CONNECTION}", headers=h)
        assert r.status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_gap_map_empty_connection_is_clean() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.get(f"/mgmt/gap-map?connection={CONNECTION}", headers=h)
        assert r.status_code == 200
        assert r.json()["gaps"] == []
    finally:
        _cleanup(org)
