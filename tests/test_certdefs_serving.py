"""The strong certified binding — exact-match serving + the trust-flag guard.

Pre-approved SQL (certified-definition page / certified answer) may reach raw tables no entity
models; the GENERATION path stays constrained to the modeled conformed tables —
so the model never "sees" or queries bronze for a question the certified doesn't cover.
"""

from __future__ import annotations

from pathlib import Path

from services.api.query import _certified_sql
from services.benchmark.contamination import normalize
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime import sql_guard

PAGE = """---
metric: net_revenue
question: What is our total net invoiced amount in euros?
sources:
  - bronze.netvisor__invoices
sql: |
  SELECT SUM(net_amount_eur) AS n FROM bronze.netvisor__invoices
---
body
"""


def _model() -> SemanticModel:
    return SemanticModel(
        lens="l",
        dialect="snowflake",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection="c", table="silver.fct_invoices"),
                fields=[Field(name="net_amount_eur", type="number")],
            )
        ],
    )


def test_certdef_certified_serves_on_exact_normalized_match(tmp_path: Path):
    (tmp_path / "01.md").write_text(PAGE, encoding="utf-8")
    assert _certified_sql(str(tmp_path), "What is our total net invoiced amount in euros?")
    assert _certified_sql(str(tmp_path), "what is our TOTAL net invoiced amount in euros")
    assert _certified_sql(str(tmp_path), "How many customers do we have?") is None
    assert _certified_sql(None, "anything") is None


def test_generation_path_cannot_reach_bronze():
    # untrusted (generated) SQL over an unmodeled table is rejected by scope
    res = sql_guard.check("SELECT SUM(net_amount_eur) FROM bronze.netvisor__invoices", _model())
    assert not res.ok and "out of lens scope" in (res.reason or "")


def test_trusted_canon_sql_may_reach_bronze():
    # the SAME bronze SQL passes when it is a pre-approved (trusted) artifact
    res = sql_guard.check(
        "SELECT SUM(net_amount_eur) FROM bronze.netvisor__invoices", _model(), trust_tables=True
    )
    assert res.ok


def test_trust_does_not_relax_the_safety_checks():
    m = _model()
    assert not sql_guard.check("DROP TABLE bronze.x", m, trust_tables=True).ok  # DML
    assert not sql_guard.check("SELECT * FROM bronze.x", m, trust_tables=True).ok  # SELECT *
    # the reserved validation schema stays blocked even when trusted
    assert not sql_guard.check("SELECT a FROM __dst.snap_x", m, trust_tables=True).ok


def test_normalize_q_collapses_case_and_punctuation():
    assert normalize("What's our  Revenue?!") == "what s our revenue"
