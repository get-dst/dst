"""The coverage / "dbt doctor" report enumerates compiled vs skipped
constructs for the mixed jaffle fixture, with no silent gaps: every construct the compiler
skipped (the ``median_order_amount`` measure, the cross-entity ``clv_per_order`` derived
metric) shows up in the report and in the rendered text with its reason."""

from __future__ import annotations

from pathlib import Path

from services.dbt import coverage_report, render_text
from services.dbt.artifacts import load_artifacts
from services.dbt.compile import import_shared_assets

TARGET = Path(__file__).resolve().parent.parent / "fixtures" / "jaffle" / "target"


def _report():
    art = load_artifacts(TARGET)
    res = import_shared_assets(art, connection="jaffle")
    return art, res, coverage_report(art, res)


def test_lists_two_semantic_models_compiled() -> None:
    _art, _res, rep = _report()
    assert rep.semantic_models_total == 2
    assert rep.semantic_models_compiled == 2
    compiled_sms = {c.name for c in rep.compiled if c.kind == "semantic_model"}
    assert compiled_sms == {"orders", "customers"}
    # both semantic models produced a queryable grain → healthy
    assert rep.ok is True


def test_skipped_measure_and_compound_metric_are_surfaced() -> None:
    _art, _res, rep = _report()
    skipped = {(s.kind, s.name) for s in rep.skipped}
    assert ("measure", "median_order_amount") in skipped
    # a same-entity ratio now IMPORTS (depth-1); only the cross-entity derived skips
    assert ("metric", "average_order_value") not in skipped
    assert ("metric", "clv_per_order") in skipped
    # both appear as non-fatal warnings (the model still compiled)
    warnings_blob = "\n".join(rep.warnings)
    assert "median_order_amount" in warnings_blob
    assert "clv_per_order" in warnings_blob


def test_render_text_contains_skipped_names_and_reasons() -> None:
    _art, _res, rep = _report()
    text = render_text(rep)
    assert "median_order_amount" in text
    assert "unsupported aggregation 'median'" in text
    assert "clv_per_order" in text
    assert "span entities" in text
    # the honest framing the spec demands
    assert "not synced:" in text


def test_nothing_is_silently_dropped() -> None:
    _art, res, rep = _report()
    # the report mirrors result.skipped exactly — no gaps, no invented entries
    assert rep.skipped_total == len(res.skipped)
    assert [(s.kind, s.name, s.reason) for s in rep.skipped] == [
        (s.kind, s.name, s.reason) for s in res.skipped
    ]
    # every skipped construct shows up in the rendered report
    text = render_text(rep)
    for s in res.skipped:
        assert s.name in text
        assert s.reason in text


def test_counts_are_consistent() -> None:
    _art, _res, rep = _report()
    # compiled + skipped measures account for every dbt measure
    assert rep.measures_compiled + rep.measures_skipped == rep.measures_total
    assert rep.definitions_compiled == rep.metrics_total  # all metrics → definitions
    assert rep.measures_skipped >= 1
    assert rep.metrics_skipped >= 1
