"""Certified-bound lens — scope derivation, schema filtering, context, certified."""

from __future__ import annotations

from services.certdefs import CertifiedDefinition
from services.certdefs.lens import CertifiedDefLens


def _m(metric, sources, **kw):
    return CertifiedDefinition(metric=metric, sources=sources, **kw)


def test_scope_is_the_union_of_sources_plus_join_tables():
    lens = CertifiedDefLens(
        name="finance",
        metrics=[
            _m("revenue", ["bronze.invoices", "bronze.invoices_legacy"]),
            _m("overdue", ["silver.snap_ar_aging"]),
        ],
        extra_tables=("silver.dim_customers",),
    )
    assert lens.scope == frozenset(
        {
            "bronze.invoices",
            "bronze.invoices_legacy",
            "silver.snap_ar_aging",
            "silver.dim_customers",
        }
    )
    assert lens.in_scope("BRONZE.INVOICES") and not lens.in_scope("staging.invoices_raw")


def test_scoped_schema_drops_out_of_scope_tables():
    lens = CertifiedDefLens(name="f", metrics=[_m("rev", ["bronze.invoices"])])
    full = (
        "bronze.invoices(id, amount)\n"
        "staging.invoices_raw(id, amount)\n"
        "archive.invoices_2019(id)\n"
        "marketing.campaigns(id, spend)"
    )
    scoped = lens.scoped_schema(full)
    assert scoped == "bronze.invoices(id, amount)"


def test_context_and_certified_and_ground_truth():
    lens = CertifiedDefLens(
        name="f",
        metrics=[
            CertifiedDefinition(
                metric="revenue",
                question="What is revenue?",
                sources=["bronze.invoices"],
                sql="SELECT SUM(amount) FROM bronze.invoices",
                verified_value={"value": 100.0},  # type: ignore[arg-type]
                summary="net revenue",
                usage_mode="auto",
            )
        ],
    )
    assert "SELECT SUM(amount)" in lens.context("What is revenue?")
    assert lens.certified == {"What is revenue?": "SELECT SUM(amount) FROM bronze.invoices"}
    assert lens.ground_truth == {"revenue": 100.0}
