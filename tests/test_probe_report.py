"""The Definition Drift Report artifact (renderer + CLI).

The fixture is REAL output from a live Snowflake run of the drift miner
(tests/fixtures/live_findings.json): a 3-way revenue conflict with observed
numbers, long nested statements, single-number AND multi-column results. The
renderer is pure — nothing here touches the network or a database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from services.probe.__main__ import main as probe_main
from services.probe.correctness import annotate_correctness
from services.probe.drift import DriftFinding, DriftVariant
from services.probe.report import (
    draft_definition,
    render_defs_yaml,
    render_html,
    render_terminal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_findings.json"
GENERATED = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)

# The three diverging revenue totals from the live run, as the report formats them.
REVENUE_READINGS = ("41,818,677.59", "35,020,740.00", "32,717,890.00")


def _findings() -> list[DriftFinding]:
    return [DriftFinding.model_validate(f) for f in json.loads(FIXTURE.read_text())]


def _html(findings: list[DriftFinding] | None = None) -> str:
    return render_html(
        findings if findings is not None else _findings(),
        org_name="Acme Analytics Oy",
        warehouse_label="Snowflake · NORTHWIND",
        generated_at=GENERATED,
    )


def _variant(statement: str, **overrides: object) -> DriftVariant:
    base: dict[str, object] = {
        "statement": statement,
        "source_tables": ["gold.t"],
        "run_count": 2,
        "principals": ["SVC"],
        "source_tools": [],
        "distinguishing": "SUM(x); over gold.t",
    }
    base.update(overrides)
    return DriftVariant.model_validate(base)


# ── render_html ───────────────────────────────────────────────────────────────


def test_html_shows_the_diverging_numbers_large() -> None:
    html = _html()
    for reading in REVENUE_READINGS:
        assert reading in html
    # header block: org, warehouse, date, scope line
    assert "Acme Analytics Oy" in html
    assert "Snowflake · NORTHWIND" in html
    assert "12 June 2026" in html
    assert "Read-only; statements and metadata only" in html
    # exhibits are numbered, severity is marked, attribution is visible
    assert "Exhibit 01" in html
    assert "conflict" in html
    assert "NORTHWIND" in html  # principal
    assert "PythonConnector 4.6.0" in html  # source tool


def test_html_escapes_untrusted_sql() -> None:
    hostile = DriftFinding(
        metric_intent="sum of <b>revenue</b>",
        variants=[
            _variant("SELECT SUM(x) FROM t -- <script>alert('pwn')</script>"),
            _variant(
                "SELECT SUM(y) FROM t",
                distinguishing='SUM(y); filters: name = "<img src=x onerror=alert(1)>"',
            ),
        ],
        blast_radius=4,
        severity="conflict",
    )
    html = _html([hostile])
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "sum of <b>revenue</b>" not in html  # intent is escaped too


def test_html_is_self_contained() -> None:
    html = _html().lower()
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html
    assert "@import" not in html
    assert "url(" not in html
    assert "<link" not in html


def test_html_handles_error_and_multirow_variants() -> None:
    finding = DriftFinding(
        metric_intent="sum of freight",
        variants=[
            _variant("SELECT SUM(a) FROM t", observed_error="statement timed out"),
            _variant(
                "SELECT m, SUM(a) FROM t GROUP BY m",
                observed_columns=["M", "FREIGHT"],
                observed_rows=[["2026-01", 100.5], ["2026-02", 200.0]],
            ),
            _variant("SELECT SUM(b) FROM t"),  # never executed → no observations
        ],
        blast_radius=6,
        severity="conflict",
    )
    html = _html([finding])
    assert "statement timed out" in html
    assert "FREIGHT" in html and "100.50" in html and "200.00" in html
    assert "not executed" in html


def test_html_renders_empty_findings() -> None:
    html = _html([])
    assert "No definition drift" in html
    assert "Acme Analytics Oy" in html


# ── the bridge: defs import file ──────────────────────────────────────────────


def test_defs_yaml_round_trips_one_entry_per_conflict() -> None:
    findings = _findings()
    duplication = DriftFinding(
        metric_intent="sum of net (copy)",
        variants=[_variant("SELECT SUM(x) FROM a"), _variant("select sum(x) from a")],
        blast_radius=4,
        severity="duplication",
    )
    entries = yaml.safe_load(render_defs_yaml([*findings, duplication]))
    conflicts = [f for f in findings if f.severity == "conflict"]
    assert isinstance(entries, list)
    assert len(entries) == len(conflicts)  # duplication emits no definition
    for entry, finding in zip(entries, conflicts, strict=True):
        assert set(entry) == {"term", "body", "source"}
        assert entry["term"] == finding.metric_intent
        assert entry["source"] == "dst-audit"
        assert entry["body"]


def test_drafted_canon_prefers_gold_over_the_majority_bronze() -> None:
    revenue = next(f for f in _findings() if f.metric_intent == "sum of net")
    # Annotate correctness before drafting — the convention-aware canon is stamped on
    # the finding and draft_definition honours it (un-annotated findings keep the old vote).
    (annotated,) = annotate_correctness([revenue])
    draft = draft_definition(annotated)
    canon_part, rejected_part = draft["body"].split("Rejected readings")
    # the majority reading (35,020,740.00, three comparable variants) is the BRONZE
    # source union; canon takes the gold mart at the same value instead.
    assert "Canonical reading" in canon_part
    assert "gold.fct_revenue_monthly" in canon_part
    assert "35,020,740.00" in canon_part
    assert "bronze.netvisor__invoices" not in canon_part  # bronze is rejected, not canon
    # the competing readings stay visible as rejected
    assert "bronze.netvisor__invoices" in rejected_part
    assert "41,818,677.59" in rejected_part
    assert "32,717,890.00" in rejected_part


def test_bridge_appears_in_html_per_conflict_exhibit() -> None:
    findings = _findings()
    html = _html(findings)
    assert html.count("Drafted lens definition") == sum(
        1 for f in findings if f.severity == "conflict"
    )
    assert "dst-audit" in html


# ── render_terminal ───────────────────────────────────────────────────────────


def test_terminal_render_fits_the_fixture() -> None:
    out = render_terminal(_findings())
    assert "DEFINITION DRIFT AUDIT" in out
    assert "sum of net" in out
    assert "41,818,677.59" in out
    assert "CONFLICT" in out
    assert all(len(line) <= 100 for line in out.splitlines())


def test_terminal_render_handles_empty_and_error_shapes() -> None:
    assert "No definition drift" in render_terminal([])
    finding = DriftFinding(
        metric_intent="sum of freight",
        variants=[
            _variant("SELECT SUM(a) FROM t", observed_error="timeout"),
            _variant("SELECT SUM(b) FROM t"),
        ],
        blast_radius=4,
        severity="conflict",
    )
    out = render_terminal([finding])
    assert "failed: timeout" in out
    assert "not executed" in out


# ── CLI (file in, files out — never the network) ──────────────────────────────


def test_cli_renders_report_and_defs_from_findings_json(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    defs = tmp_path / "audit-defs.yaml"
    code = probe_main(
        [
            "--findings",
            str(FIXTURE),
            "--org",
            "Acme Analytics Oy",
            "--out",
            str(out),
            "--defs",
            str(defs),
            "--warehouse-label",
            "Snowflake · NORTHWIND",
        ]
    )
    assert code == 0
    html = out.read_text()
    assert "Acme Analytics Oy" in html and "41,818,677.59" in html
    entries = yaml.safe_load(defs.read_text())
    assert len(entries) == 5 and all(e["source"] == "dst-audit" for e in entries)
