"""Certified definitions — parse, round-trip, context selection, ground-truth."""

from __future__ import annotations

from pathlib import Path

from services.certdefs import (
    CertifiedDefinition,
    ground_truth,
    load_certified_defs,
    parse_definition_page,
    render_context,
    render_definition_page,
)

PAGE = """---
metric: net_invoiced_revenue
summary: Net invoiced revenue in euros, era-unioned and deduped
owner: finance
grain: invoice_id
sources:
  - bronze.netvisor__invoices
  - bronze.netvisor__invoices_legacy
source_of_truth: "notion://finance/revenue#net-invoiced"
usage_mode: auto
verified_value:
  value: 35020740.0
  as_of: "2026-06-15"
sql: |
  SELECT SUM(net_amount_eur) FROM unioned_invoices
---

Net invoiced revenue sums net amounts across BOTH billing eras, deduped by
latest _fivetran_synced per invoice_id, excluding test customers.

## Rejected readings
- Summing silver.fct_invoices directly misses the legacy era (wrong).
"""


def test_parse_extracts_frontmatter_and_body():
    m = parse_definition_page(PAGE)
    assert m.metric == "net_invoiced_revenue" and m.owner == "finance"
    assert m.grain == "invoice_id"
    assert m.sources == ["bronze.netvisor__invoices", "bronze.netvisor__invoices_legacy"]
    assert m.verified_value is not None and m.verified_value.value == 35020740.0
    assert "Rejected readings" in m.body
    assert "SELECT SUM" in (m.sql or "")


def test_round_trips_through_disk_form():
    m = parse_definition_page(PAGE)
    again = parse_definition_page(render_definition_page(m))
    assert again.metric == m.metric and again.verified_value == m.verified_value
    assert again.sources == m.sources and again.body == m.body


def test_term_and_sql_expr_are_accepted_as_aliases():
    """Authors (and their agents) write the names the REST of the product uses —
    `term:` (Definition.term, lens.yaml's definitions: list) and `sql_expr:`
    (Definition.sql_expr). Both were silently dropped or rejected: `term:` gave
    "field required: metric", `sql_expr:` vanished under extra=ignore and the
    term went unenforced. Both spellings are what the product's own vocabulary
    teaches, so both are accepted."""
    page = (
        "---\nterm: repeat_customer\nsql_expr: customers.number_of_orders > 1\n"
        "---\n\nMore than one order.\n"
    )
    m = parse_definition_page(page)
    assert m.metric == "repeat_customer"
    assert m.sql == "customers.number_of_orders > 1"
    # The canonical render is unchanged, so the plan's parse-then-re-render
    # canonicalization matches an aliased page against the DB — no phantom diff.
    rendered = render_definition_page(m, minimal=True)
    assert "metric: repeat_customer" in rendered and "sql: customers" in rendered
    assert "term:" not in rendered and "sql_expr:" not in rendered
    assert parse_definition_page(rendered).metric == m.metric


def test_field_names_remain_valid_kwargs():
    """populate_by_name: in-code construction keeps using the field names."""
    m = CertifiedDefinition(metric="x", sql="SELECT 1")
    assert m.metric == "x" and m.sql == "SELECT 1"


def test_usage_mode_selects_context():
    auto = CertifiedDefinition(metric="a", summary="always", sql="SELECT 1", usage_mode="auto")
    searchable = CertifiedDefinition(metric="b", summary="on demand", usage_mode="search")
    ctx = render_context([auto, searchable])  # default: auto only
    assert "always" in ctx and "on demand" not in ctx
    both = render_context([auto, searchable], modes=("auto", "search"))
    assert "always" in both and "on demand" in both


def test_render_includes_grain_and_sql_as_exemplar():
    m = CertifiedDefinition(metric="rev", summary="s", grain="invoice_id", sql="SELECT SUM(x)")
    ctx = render_context([m])
    assert "grain: invoice_id" in ctx and "canonical SQL:" in ctx and "SELECT SUM(x)" in ctx
    no_sql = render_context([m], include_sql=False)
    assert "canonical SQL:" not in no_sql


def test_ground_truth_collects_known_assertions():
    metrics = [
        CertifiedDefinition(metric="rev", verified_value={"value": 35020740.0}),  # type: ignore[arg-type]
        CertifiedDefinition(metric="count", verified_value={"value": 30}),  # type: ignore[arg-type]
        CertifiedDefinition(metric="undocumented"),  # no verified value
    ]
    gt = ground_truth(metrics)
    assert gt == {"rev": 35020740.0, "count": 30}


def test_load_canon_reads_a_directory(tmp_path: Path):
    (tmp_path / "01_rev.md").write_text(PAGE, encoding="utf-8")
    (tmp_path / "02_min.md").write_text("---\nmetric: customers\n---\nbody", encoding="utf-8")
    metrics = load_certified_defs(tmp_path)
    assert [m.metric for m in metrics] == ["net_invoiced_revenue", "customers"]


def test_select_context_retrieves_relevant_search_pages():
    from services.certdefs import select_context

    metrics = [
        CertifiedDefinition(metric="always_on", summary="standing rule", usage_mode="auto"),
        CertifiedDefinition(
            metric="revenue",
            summary="net invoiced revenue",
            question="What is revenue?",
            usage_mode="search",
            sql="SELECT SUM(x)",
        ),
        CertifiedDefinition(
            metric="headcount",
            summary="employee count",
            question="How many staff?",
            usage_mode="search",
            sql="SELECT COUNT(*)",
        ),
    ]
    ctx = select_context(metrics, "What is our revenue this year?", k=1)
    assert "standing rule" in ctx  # auto always included
    assert "net invoiced revenue" in ctx  # relevant search page retrieved
    assert "employee count" not in ctx  # irrelevant search page excluded
