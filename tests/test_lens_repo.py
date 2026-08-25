"""Materializer for the lens-as-repo file tree (services/lenses/repo.py).

Pure rendering — no DB. Pins: LOCAL definitions become certified-format pages
that round-trip (shared terms stay at project scope), queries.yaml carries the
lens's voice, compiled.yaml is the full browseable model, and optional
artifacts (certified / evals / audit) only appear when supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime

import yaml

from services.api.audit_store import AuditRun
from services.certdefs import CertifiedDefinition, parse_definition_page
from services.contracts.semantic_model import Definition
from services.evals.store import EvalCaseRow
from services.lenses.demo import jaffle_customer_value_bundle
from services.lenses.repo import render_lens_repo
from services.lenses.store import LensBundle


def _bundle_with_local_definition() -> LensBundle:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(
            term="board_margin", body="A board margin has margin > 0.", sql_expr="margin > 0"
        )
    )
    return bundle


def test_core_files_and_counts() -> None:
    files = render_lens_repo(jaffle_customer_value_bundle())
    assert "README.md" in files
    assert "queries.yaml" in files
    assert "compiled.yaml" in files
    assert "semantic_model.yaml" not in files
    # both demo definitions are SHARED — they render at project scope, not here
    assert not any(p.startswith("definitions/") for p in files)
    assert "- entities: 2" in files["README.md"]
    assert "- definitions: 3" in files["README.md"]


def test_only_local_definitions_render_as_pages() -> None:
    files = render_lens_repo(_bundle_with_local_definition())
    assert [p for p in files if p.startswith("definitions/")] == ["definitions/board-margin.md"]


def test_definition_pages_roundtrip_as_canon() -> None:
    files = render_lens_repo(_bundle_with_local_definition())
    page = parse_definition_page(files["definitions/board-margin.md"])
    assert page.metric == "board_margin"
    assert page.body == "A board margin has margin > 0."
    assert page.sql == "margin > 0"  # Definition.sql_expr -> CertifiedDefinition.sql


def test_definition_frontmatter_is_minimal() -> None:
    # No certified boilerplate on a plain definition — only the fields it actually carries.
    page = render_lens_repo(_bundle_with_local_definition())["definitions/board-margin.md"]
    for noise in ("owner:", "usage_mode:", "summary:", "sources:"):
        assert noise not in page, f"unexpected certified default {noise!r} in definition page"
    assert page.startswith("---\nmetric: board_margin\n")


def test_queries_yaml_carries_the_lens_voice() -> None:
    files = render_lens_repo(jaffle_customer_value_bundle())
    data = yaml.safe_load(files["queries.yaml"])
    assert set(data) == {"use_when", "sample_queries"}
    assert data["sample_queries"][0]["question"] == "How many orders were placed in total?"


def test_compiled_yaml_is_the_full_model() -> None:
    files = render_lens_repo(jaffle_customer_value_bundle())
    sm = yaml.safe_load(files["compiled.yaml"])
    assert {e["name"] for e in sm["entities"]} == {"customers", "orders"}
    assert sm["dialect"] == "duckdb"
    assert {d["term"] for d in sm["definitions"]} == {"lifetime_value", "repeat_customer", "value"}
    assert sm["shared_provenance"]["assets"]  # the staleness signal is browseable


def test_optional_artifacts_omitted_when_absent() -> None:
    files = render_lens_repo(jaffle_customer_value_bundle())
    assert not any(p.startswith(("certified/", "audit/")) for p in files)
    # evals/cases.yaml renders even empty (like certified_answers.yaml): the
    # scaffold ships an authored [] file that must not phantom-diff forever.
    assert files["evals/cases.yaml"] == "[]\n"
    assert "## Freshness" not in files["README.md"]


def test_optional_artifacts_present_when_supplied() -> None:
    certified = [
        CertifiedDefinition(metric="net_revenue", summary="invoiced minus credits", body="…")
    ]
    cases = [
        EvalCaseRow(
            id="1",
            lens="customer_value",
            question="how many repeat customers?",
            expected_sql="SELECT count(*) FROM customers WHERE number_of_orders > 1",
            expected_answer="42",
            snapshot_ref=None,
            source="harvested",
            status="approved",
            created_by="alex",
        )
    ]
    audit = AuditRun(
        id="a1",
        connection="jaffle",
        days=30,
        records_scanned=128,
        findings=[],
        status="ok",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
    )
    files = render_lens_repo(
        jaffle_customer_value_bundle(), certified=certified, eval_cases=cases, audit=audit
    )
    assert "certified/net-revenue.md" in files
    assert yaml.safe_load(files["evals/cases.yaml"])[0]["question"] == "how many repeat customers?"
    assert '"connection": "jaffle"' in files["audit/latest.json"]
    assert "## Freshness" in files["README.md"]
