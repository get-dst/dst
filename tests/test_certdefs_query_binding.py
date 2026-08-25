"""Certified bound into the query runtime as a context source."""

from __future__ import annotations

from pathlib import Path

from services.api.query import _certified_context

PAGE = """---
metric: net_revenue
summary: net invoiced revenue, era-unioned and deduped
sql: |
  SELECT SUM(net_amount_eur) FROM unioned_invoices
usage_mode: auto
---
Revenue sums both billing eras, deduped by latest _fivetran_synced.
"""


def test_certdef_context_injects_rules_and_sql(tmp_path: Path):
    (tmp_path / "01_rev.md").write_text(PAGE, encoding="utf-8")
    chunk = _certified_context(str(tmp_path), "What was our revenue?")
    assert chunk is not None and chunk.source == "certified"
    assert "both billing eras" in chunk.text and "SELECT SUM" in chunk.text


def test_no_canon_dir_is_a_clean_none():
    assert _certified_context(None, "anything") is None


def test_bad_canon_path_never_breaks_a_query(tmp_path: Path):
    # a non-existent dir must not raise — a query never dies on certified
    assert _certified_context(str(tmp_path / "nope"), "q") is None
