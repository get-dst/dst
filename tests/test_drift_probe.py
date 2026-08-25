"""The wrongness probe — query history → Definition Drift Report.

The synthetic records mirror the drift actually planted in the Snowflake env:
three conflicting revenue definitions across three gold tables, COUNT(*) vs
COUNT(DISTINCT) on invoices, plus noise. Everything runs offline — ScriptedLLM
for naming, a DuckDB mini-world for the execute-variants path.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.benchmark.world import load_world
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.query_history import QueryRecord
from services.contracts.warehouse import QueryResult
from services.definitions.standards import OrgStandard
from services.probe.drift import DriftFinding, DriftVariant, execute_variants, mine_drift
from services.probe.report import annotate_against_canon


def _rec(statement: str, runs: int, who: str | None = None, tool: str | None = None) -> QueryRecord:
    return QueryRecord(
        statement=statement,
        first_seen=datetime(2026, 5, 1, tzinfo=UTC),
        last_seen=datetime(2026, 6, 10, tzinfo=UTC),
        run_count=runs,
        principal=who,
        source_tool=tool,
    )


# The planted revenue drift: one business metric, three tables, two column names.
REVENUE_RECORDS = [
    _rec(
        "SELECT DATE_TRUNC('month', invoice_date) AS month, SUM(net_revenue_eur) AS revenue "
        "FROM gold.fct_revenue_monthly WHERE fiscal_year = 2025 GROUP BY 1",
        25,
        "ANALYST_1",
        "Omni",
    ),
    _rec(  # literal variant of the definition above — must fold into ONE variant
        "SELECT DATE_TRUNC('month', invoice_date) AS month, SUM(net_revenue_eur) AS revenue "
        "FROM gold.fct_revenue_monthly WHERE fiscal_year = 2024 GROUP BY 1",
        15,
        "ANALYST_2",
        "Omni",
    ),
    _rec(
        "SELECT DATE_TRUNC('month', invoice_date) AS month, SUM(revenue_eur) AS revenue "
        "FROM gold.sales_revenue_monthly GROUP BY 1",
        22,
        "CFO",
        "Tableau",
    ),
    _rec("SELECT SUM(net_revenue_eur) FROM gold.dashboard_revenue_old", 5, "ANALYST_1", "Excel"),
]

INVOICE_RECORDS = [
    _rec("SELECT COUNT(*) AS n FROM silver.fct_invoices", 30, "OPS", "Metabase"),
    _rec(
        "SELECT COUNT(DISTINCT invoice_id) AS n FROM silver.fct_invoices", 12, "ANALYST_1", "Omni"
    ),
]

NOISE_RECORDS = [
    _rec("SELECT SUM(freight_cost_eur) AS c FROM gold.fct_shipments", 9),  # singleton intent
    _rec("SELECT customer_name FROM crm.dim_customers LIMIT 10", 50),  # no aggregate
    _rec("TOTALLY NOT ((VALID SQL", 3),  # unparseable — skipped, not guessed at
]


def test_revenue_drift_is_one_finding_with_three_variants() -> None:
    (finding,) = mine_drift(REVENUE_RECORDS)
    assert finding.severity == "conflict"
    assert finding.blast_radius == 67
    assert len(finding.variants) == 3
    # literal variants (fiscal_year 2025/2024) folded; ordered by run_count desc
    assert [v.run_count for v in finding.variants] == [40, 22, 5]
    assert [v.source_tables for v in finding.variants] == [
        ["gold.fct_revenue_monthly"],
        ["gold.sales_revenue_monthly"],
        ["gold.dashboard_revenue_old"],
    ]
    # the fold aggregates principals/tools across literal variants
    assert finding.variants[0].principals == ["ANALYST_1", "ANALYST_2"]
    assert finding.variants[0].source_tools == ["Omni"]
    # each variant says what it does differently: its measure and its table
    assert "SUM(net_revenue_eur)" in finding.variants[0].distinguishing
    assert "over gold.sales_revenue_monthly" in finding.variants[1].distinguishing
    assert "SUM(revenue_eur)" in finding.variants[1].distinguishing


def test_count_star_vs_count_distinct_is_one_finding() -> None:
    (finding,) = mine_drift(INVOICE_RECORDS)
    assert finding.severity == "conflict"
    assert finding.blast_radius == 42
    assert len(finding.variants) == 2
    assert "COUNT(*)" in finding.variants[0].distinguishing
    assert "COUNT(DISTINCT invoice_id)" in finding.variants[1].distinguishing


def test_full_corpus_yields_exactly_the_planted_findings() -> None:
    findings = mine_drift(REVENUE_RECORDS + INVOICE_RECORDS + NOISE_RECORDS)
    # noise: singleton intent < min_cluster, no-aggregate and unparseable skipped
    assert [f.blast_radius for f in findings] == [67, 42]  # blast-radius order
    assert findings[0].metric_intent == "sum of revenue"
    assert findings[1].metric_intent == "count of invoice"


def test_min_cluster_gates_distinct_definitions_not_run_counts() -> None:
    findings = mine_drift(REVENUE_RECORDS + INVOICE_RECORDS, min_cluster=3)
    (finding,) = findings  # invoices (2 variants) drops out; revenue (3) stays
    assert len(finding.variants) == 3


def test_equivalent_rewrites_are_duplication_not_conflict() -> None:
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total_revenue FROM gold.fct_revenue_monthly", 4),
        _rec("SELECT SUM(net_revenue_eur) AS monthly_total FROM gold.fct_revenue_monthly", 3),
    ]
    (finding,) = mine_drift(records)
    assert finding.severity == "duplication"
    assert all("equivalent definition" in v.distinguishing for v in finding.variants)


def test_llm_names_findings_in_blast_radius_order() -> None:
    llm = ScriptedLLM(['{"name": "Monthly net revenue"}', '{"name": "Invoice volume"}'])
    findings = mine_drift(REVENUE_RECORDS + INVOICE_RECORDS, llm)
    assert [f.metric_intent for f in findings] == ["Monthly net revenue", "Invoice volume"]


def test_llm_garbage_keeps_the_deterministic_name() -> None:
    findings = mine_drift(INVOICE_RECORDS, ScriptedLLM(["not json at all"]))
    assert findings[0].metric_intent == "count of invoice"


# ── execute_variants against a DuckDB mini-world ─────────────────────────────


@pytest.fixture()
def drift_world(tmp_path: Path) -> Path:
    """Three gold revenue tables whose totals genuinely diverge: 1000 / 1180 / 900."""
    gold = tmp_path / "csv" / "gold"
    gold.mkdir(parents=True)
    gold.joinpath("fct_revenue_monthly.csv").write_text(
        "month,net_revenue_eur\n2026-01-01,600.0\n2026-02-01,400.0\n", encoding="utf-8"
    )
    gold.joinpath("sales_revenue_monthly.csv").write_text(
        "month,revenue_eur\n2026-01-01,700.0\n2026-02-01,480.0\n", encoding="utf-8"
    )
    gold.joinpath("dashboard_revenue_old.csv").write_text(
        "month,net_revenue_eur\n2026-01-01,540.0\n2026-02-01,360.0\n", encoding="utf-8"
    )
    db = tmp_path / "world.duckdb"
    load_world(tmp_path / "csv", db)
    return db


class _SpyConnector:
    """Wraps DuckDBConnector, recording every execute call's read_only/row_limit."""

    def __init__(self, inner: DuckDBConnector) -> None:
        self.kind = inner.kind
        self._inner = inner
        self.calls: list[tuple[str, bool, int | None]] = []

    def introspect(self):  # Connector protocol passthrough
        return self._inner.introspect()

    def dry_run(self, sql: str):
        return self._inner.dry_run(sql)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        self.calls.append((sql, read_only, row_limit))
        return self._inner.execute(sql, read_only=read_only, row_limit=row_limit)


def test_execute_variants_attaches_the_diverging_numbers(drift_world: Path) -> None:
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 25),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22),
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.dashboard_revenue_old", 5),
    ]
    (finding,) = mine_drift(records, dialect="duckdb")
    spy = _SpyConnector(DuckDBConnector(str(drift_world)))
    enriched = execute_variants(spy, finding)

    values = [v.observed_rows[0][0] for v in enriched.variants if v.observed_rows]
    assert values == [1000.0, 1180.0, 900.0]  # three definitions, three answers
    assert len(set(values)) == 3
    assert all(v.observed_error is None for v in enriched.variants)
    # every call went through the read-only, row-capped seam
    assert len(spy.calls) == 3
    assert all(read_only is True for _, read_only, _ in spy.calls)
    assert all(limit is not None and limit <= 10 for _, _, limit in spy.calls)
    # the input finding is untouched (execute_variants is pure)
    assert all(v.observed_rows is None for v in finding.variants)


def test_execute_variants_never_issues_writes(drift_world: Path) -> None:
    finding = DriftFinding(
        metric_intent="sum of revenue",
        severity="conflict",
        blast_radius=2,
        variants=[
            DriftVariant(
                statement="DROP TABLE gold.fct_revenue_monthly",
                source_tables=["gold.fct_revenue_monthly"],
                run_count=1,
                distinguishing="ddl smuggled into a finding",
            ),
            DriftVariant(
                statement=(
                    "SELECT SUM(net_revenue_eur) AS t FROM gold.fct_revenue_monthly; "
                    "DELETE FROM gold.fct_revenue_monthly"
                ),
                source_tables=["gold.fct_revenue_monthly"],
                run_count=1,
                distinguishing="multi-statement injection",
            ),
        ],
    )
    spy = _SpyConnector(DuckDBConnector(str(drift_world)))
    out = execute_variants(spy, finding)
    assert spy.calls == []  # nothing reached the warehouse
    assert all(v.observed_error and "not run" in v.observed_error for v in out.variants)
    assert all(v.observed_rows is None for v in out.variants)


# ── match the audit's canon against the org's declared standards ───────────────


def _variant(distinguishing: str, table: str, runs: int) -> DriftVariant:
    return DriftVariant(
        statement=f"SELECT {distinguishing}",
        source_tables=[table],
        run_count=runs,
        distinguishing=distinguishing,
    )


def _conflict(metric: str, variants: list[DriftVariant]) -> DriftFinding:
    return DriftFinding(
        metric_intent=metric,
        variants=variants,
        blast_radius=sum(v.run_count for v in variants),
        severity="conflict",
    )


_NET = _variant("SUM(net_revenue_eur)", "gold.fct_revenue", 40)
_NET_LOW = _variant("SUM(net_revenue_eur)", "gold.fct_revenue", 22)
_GROSS = _variant("SUM(revenue_eur)", "gold.sales_revenue", 40)
_GROSS_LOW = _variant("SUM(revenue_eur)", "gold.sales_revenue", 22)

# The popular reading (index 0, most-run) IS the declared one.
_AGREES = _conflict("monthly revenue", [_NET, _GROSS_LOW])
# The popular reading is NOT the declared one — the sharpest signal.
_CONTRADICTS = _conflict("monthly revenue", [_GROSS, _NET_LOW])
_DECLARED = OrgStandard(term="revenue", body="Net revenue in EUR.", sql_expr="SUM(net_revenue_eur)")


def test_canon_match_agrees_when_popular_reading_is_declared() -> None:
    (out,) = annotate_against_canon([_AGREES], [_DECLARED])
    assert out.canon_match is not None
    assert out.canon_match.term == "revenue"
    assert out.canon_match.state == "agrees"


def test_canon_match_contradicts_when_popular_reading_is_not_declared() -> None:
    (out,) = annotate_against_canon([_CONTRADICTS], [_DECLARED])
    assert out.canon_match is not None
    assert out.canon_match.state == "contradicts"  # the popular one isn't the declared one


def test_canon_match_none_when_nothing_declared_for_the_metric() -> None:
    churn = OrgStandard(term="churn", body="Logo churn.", sql_expr="COUNT(*)")
    assert annotate_against_canon([_AGREES], [churn])[0].canon_match is None
    assert annotate_against_canon([_AGREES], [])[0].canon_match is None  # no standards at all


def test_canon_match_undetermined_for_a_body_only_standard() -> None:
    body_only = OrgStandard(term="revenue", body="Net revenue.", sql_expr=None)
    (out,) = annotate_against_canon([_AGREES], [body_only])
    assert out.canon_match is not None
    assert out.canon_match.state == "undetermined"  # a standard exists but no SQL to align


# ── regressions from real BigQuery query history ─────────────────────────────


def test_star_wrapped_aggregate_is_still_mined() -> None:
    """Much of real query history arrives as `SELECT * FROM (<agg>) AS _q LIMIT n`
    (the BI-tool/row-cap wrapper), which the miner must see through."""
    records = [
        _rec(
            "SELECT * FROM (SELECT SUM(net_revenue_eur) AS total "
            "FROM gold.fct_revenue_monthly) AS _q LIMIT 100",
            30,
        ),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22),
    ]
    (finding,) = mine_drift(records)
    assert len(finding.variants) == 2
    assert finding.blast_radius == 52


def test_countif_ratio_is_a_metric_intent() -> None:
    """BigQuery win-rate lives on COUNTIF, which is not a Count subclass in sqlglot."""
    records = [
        _rec(
            "SELECT COUNTIF(status = 'Won') / COUNTIF(status IN ('Won', 'Lost')) AS win_rate "
            "FROM sales.quotes",
            9,
        ),
        _rec("SELECT COUNTIF(status = 'Won') / COUNT(*) AS win_rate FROM sales.quotes", 4),
    ]
    (finding,) = mine_drift(records, dialect="bigquery")
    assert len(finding.variants) == 2


def test_project_dataset_tokens_do_not_glue_metrics() -> None:
    """Catalog/dataset tokens sit in every BigQuery FQN; tokenizing whole FQNs merged
    every same-family metric in the project into one mega-cluster."""
    records = [
        _rec("SELECT COUNT(*) AS n FROM `vlvi.crm.leads`", 5),
        _rec("SELECT COUNT(DISTINCT lead_id) AS n FROM `vlvi.crm.leads`", 3),
        _rec("SELECT COUNT(*) AS n FROM `vlvi.sales.quotes`", 4),
        _rec("SELECT COUNT(DISTINCT quote_id) AS n FROM `vlvi.sales.quotes`", 2),
    ]
    findings = mine_drift(records, dialect="bigquery")
    assert len(findings) == 2  # lead counts and quote counts stay apart


def test_report_join_queries_do_not_bridge_clusters() -> None:
    """A 3+-table funnel report overlaps tokens with every metric it consumes; under
    union-find it transitively glued unrelated clusters into one 28-variant finding."""
    records = [
        _rec("SELECT COUNT(*) AS n FROM crm.leads", 5),
        _rec("SELECT COUNT(DISTINCT lead_id) AS n FROM crm.leads", 3),
        _rec("SELECT COUNT(*) AS n FROM sales.quotes", 4),
        _rec("SELECT COUNT(DISTINCT quote_id) AS n FROM sales.quotes", 2),
        _rec(
            "SELECT COUNT(*) AS n FROM crm.leads l "
            "JOIN sales.quotes q ON q.lead_id = l.lead_id "
            "JOIN finance.invoices i ON i.quote_id = q.quote_id",
            7,
        ),
    ]
    findings = mine_drift(records)
    assert len(findings) == 2  # the funnel is its own singleton intent, below min_cluster


def test_trailing_semicolon_variants_still_execute(drift_world: Path) -> None:
    """A third of live history ended in `;`; the connectors' row-cap wrapper turned
    each into `SELECT * FROM (...;) AS _q LIMIT n` — a syntax error."""
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly;", 25),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly;", 22),
    ]
    (finding,) = mine_drift(records, dialect="duckdb")
    enriched = execute_variants(_SpyConnector(DuckDBConnector(str(drift_world))), finding)
    assert all(v.observed_error is None for v in enriched.variants)
    assert [v.observed_rows[0][0] for v in enriched.variants if v.observed_rows] == [1000.0, 1180.0]


# ── regressions from the second live evaluation (junk-drawer round) ──────────


def test_wrapped_and_raw_twins_fold_into_one_variant() -> None:
    """The same statement run raw and through the row-cap wrapper is ONE definition,
    not two — wrapped twins double-counted every variant on the live warehouse."""
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 20),
        _rec(
            "SELECT * FROM (SELECT SUM(net_revenue_eur) AS total "
            "FROM gold.fct_revenue_monthly) AS _q LIMIT 5",
            10,
        ),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22),
    ]
    (finding,) = mine_drift(records)
    assert len(finding.variants) == 2  # twins folded; the real second definition remains
    assert finding.blast_radius == 52
    assert max(v.run_count for v in finding.variants) == 30  # 20 raw + 10 wrapped


def test_report_queries_do_not_glue_to_metric_statements() -> None:
    """A multi-aggregate breakdown report consumes several metrics at once; it must
    not merge into a metric's cluster (the 24-variant junk-drawer mechanism)."""
    records = [
        # the real war: churned accounts with vs without DISTINCT
        _rec("SELECT COUNT(DISTINCT account_name) AS churned_accounts FROM sales.deals", 5),
        _rec("SELECT COUNT(account_name) AS churned_accounts FROM sales.deals", 3),
        # exploratory reports over the same table — 3+ aggregates each
        _rec(
            "SELECT segment, COUNT(*) AS deals, SUM(contract_value_eur) AS tcv, "
            "AVG(contract_value_eur) AS avg_deal FROM sales.deals GROUP BY segment",
            9,
        ),
        _rec(
            "SELECT service, COUNT(*) AS deals, SUM(contract_value_eur) AS value_eur, "
            "MAX(close_date) AS latest FROM sales.deals GROUP BY service",
            4,
        ),
    ]
    (finding,) = mine_drift(records)
    assert len(finding.variants) == 2
    assert all("churned_accounts" in v.statement for v in finding.variants)


def test_same_report_evolved_twice_still_folds() -> None:
    """Two versions of the SAME report (same table, same breakdown, shared output
    names) are real drift and must still cluster."""
    records = [
        _rec(
            "SELECT source, COUNT(*) AS leads, "
            "ROUND(COUNTIF(status = 'Won') / COUNT(*), 3) AS conv "
            "FROM crm.leads GROUP BY source",
            5,
        ),
        _rec(  # the older version carries an extra output column — a distinct shape
            "SELECT source, COUNT(*) AS leads, COUNTIF(status = 'Voitettu') AS won, "
            "ROUND(COUNTIF(status = 'Voitettu') / COUNT(*), 3) AS conv "
            "FROM crm.leads GROUP BY source",
            3,
        ),
    ]
    (finding,) = mine_drift(records, dialect="bigquery")
    assert len(finding.variants) == 2


def test_bare_count_identity_is_alias_not_table() -> None:
    """COUNT(*) measures nothing by name: its identity is what the author calls it,
    so differently-named counts over one table stay separate intents."""
    records = [
        _rec("SELECT COUNT(*) AS expired_contracts FROM sales.deals WHERE status = 'x'", 4),
        _rec("SELECT COUNT(*) AS expired_contracts FROM sales.deals WHERE status = 'y' OR 1=1", 2),
        _rec("SELECT COUNT(*) AS won_deals FROM sales.deals WHERE won = TRUE", 6),
    ]
    findings = mine_drift(records)
    assert len(findings) == 1  # the two expired variants; won_deals is its own intent
    assert all("expired_contracts" in v.statement for v in findings[0].variants)


def test_values_agree_annotation(drift_world: Path) -> None:
    """A conflict whose variants produce identical numbers today is latent, not live."""
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 25),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22),
    ]
    (finding,) = mine_drift(records, dialect="duckdb")
    assert finding.values_agree is None  # not executed yet
    enriched = execute_variants(_SpyConnector(DuckDBConnector(str(drift_world))), finding)
    assert enriched.values_agree is False  # 1000.0 vs 1180.0 — a live conflict

    same = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 25),
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly WHERE 1 = 1", 3),
    ]
    (latent,) = mine_drift(same, dialect="duckdb")
    agreed = execute_variants(_SpyConnector(DuckDBConnector(str(drift_world))), latent)
    assert agreed.values_agree is True  # definitions differ, numbers agree today


def test_values_agree_compares_numbers_not_projections(drift_world: Path) -> None:
    """Two definitions returning identical totals through different dimension
    columns are in latent agreement — whole-row equality would almost never fire."""
    records = [
        _rec("SELECT 'fct' AS src, SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 9),
        _rec(
            "SELECT SUM(net_revenue_eur) AS the_total FROM gold.fct_revenue_monthly WHERE 1 = 1", 2
        ),
    ]
    (finding,) = mine_drift(records, dialect="duckdb")
    enriched = execute_variants(_SpyConnector(DuckDBConnector(str(drift_world))), finding)
    assert enriched.values_agree is True  # same 1000.0 either way; 'fct' label ignored


def test_llm_duplicate_names_are_retried_then_disambiguated() -> None:
    """Two findings must not share one name in a report; a duplicate triggers one
    retry (with the taken names in the prompt) and then a deterministic suffix."""
    records = [
        _rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 25),
        _rec("SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly", 22),
        _rec("SELECT SUM(freight_cost_eur) AS freight FROM gold.fct_shipments", 9),
        _rec("SELECT SUM(freight_eur) AS freight FROM gold.shipments_old", 3),
    ]
    llm = ScriptedLLM(
        [
            '{"name": "Revenue"}',  # finding 1
            '{"name": "Revenue"}',  # finding 2 first try — duplicate
            '{"name": "Revenue"}',  # finding 2 retry — still stubborn
        ]
    )
    findings = mine_drift(records, llm=llm)
    names = [f.metric_intent for f in findings]
    assert names == ["Revenue", "Revenue (2)"]
