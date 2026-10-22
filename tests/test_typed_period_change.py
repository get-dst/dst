"""A change asked in words is a period comparison; a ratio reads a period.

"How did fee revenue change", "MoM growth", "this month vs last month" were
served as the level of one window. On the typed path they now build
``compare`` from the question's own words (services/runtime/growth.py), and a
change over no stated period asks for one, offering the last complete period.
A ratio asked over no period reads the last complete month and says so,
instead of blending every period of the table. "net new" asked of a
one-sided count asks which net figure.
"""

from __future__ import annotations

import pytest

from services.contracts.semantic_model import Metric
from services.runtime import growth
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import pin_intent, sole_metric_intent
from tests.test_typed_question_shapes import TODAY, _members, _model, _resolve, _run

_DAY_PASS = Metric(name="day_pass_visits", agg="count", filters=["visits.pass_type = 'day pass'"])
_SHARE = Metric(
    name="day_pass_share",
    type="ratio",
    numerator="day_pass_visits",
    denominator="visit_count",
    label="day pass share",
)


def _fee(question: str, *prefer: str):  # type: ignore[no-untyped-def]
    return _resolve(question, _model(), "aggregate", "fee_revenue", *prefer)


# ── a change is a comparison ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "How did fee revenue change in September 2026?",
        "MoM fee revenue growth in September 2026",
        "Fee revenue in September 2026 vs August 2026",
        "Fee revenue in September 2026 compared to August 2026",
    ],
)
def test_a_change_compares_september_with_august(question: str) -> None:
    res = _fee(question)
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None, question
    rows = _run(compile_intent(res.intent, _model()))
    # September 12+12+45+90, August 45+12
    assert rows == [(159.0, 57.0, 102.0, pytest.approx(102 / 57))], question
    assert any(d.slot == "compare" for d in res.decisions)


def test_this_month_vs_last_month_compares_the_two_named_windows() -> None:
    res = _fee("Fee revenue this month vs last month")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None
    assert res.intent.compare.to == "2026-09-01/2026-09-30"
    assert [(f.op, f.value) for f in res.intent.filters] == [
        (">=", "2026-10-01"),
        ("<=", "2026-10-22"),
    ]


def test_a_period_read_to_date_compares_the_same_days_before() -> None:
    res = _fee("How did fee revenue change this month?")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None
    assert res.intent.compare.to == "2026-09-01/2026-09-22"


def test_the_same_month_last_year_is_a_year_on_year_comparison() -> None:
    res = _fee("Fee revenue this month compared to the same month last year")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None and res.intent.compare.to == "previous_year"


def test_a_unit_word_over_a_longer_window_is_that_units_series() -> None:
    res = _fee("Fee revenue MoM growth in Q3 2026")
    assert res.intent is not None, res.clarification
    assert res.intent.grain == "month"
    assert res.intent.compare is not None
    assert res.intent.compare.to == "2026-06-01/2026-08-31"
    compile_intent(res.intent, _model())  # the buckets line up


def test_a_change_with_no_period_offers_the_last_complete_one() -> None:
    for question in ("What is our fee revenue growth?", "MoM fee revenue growth"):
        res = _fee(question)
        assert res.intent is None and res.clarification is not None, question
        assert res.clarification.term == "window"
        assert res.clarification.options == ["September 2026 vs August 2026"]


def test_only_the_comparison_window_named_clarifies() -> None:
    res = _fee("How did fee revenue change vs last month?")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "window"


def test_a_ranking_of_a_change_orders_by_the_delta() -> None:
    model = _model()
    res = _resolve(
        "Which gym grew fee revenue the most in September 2026?",
        model,
        "ranking",
        "fee_revenue",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["gym"]
    assert [(o.field, o.dir) for o in res.intent.order_by] == [("fee_revenue_delta", "desc")]
    rows = _run(compile_intent(res.intent, model))
    # old mill 135 vs 45, harbour wall 24 vs 12
    assert [(r[0], r[3]) for r in rows] == [("old mill", 90.0), ("harbour wall", 12.0)]


def test_a_metric_that_is_the_change_reads_its_window_plainly() -> None:
    change = Metric(name="fee_change", agg="sum", expr="visits.fee", label="fee change")
    res = _resolve(
        "What was the fee change in September 2026?", _model(change), "aggregate", "fee_change"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.compare is None


def test_versus_between_two_figures_is_not_a_period_comparison() -> None:
    res = _fee("Fee revenue vs visits in September 2026")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is None


def test_net_new_asked_of_a_one_sided_count_clarifies() -> None:
    model = _model(members=True)
    model = model.model_copy(update={"entities": [_members()]})
    res = _resolve(
        "How many net new members joined in September 2026?", model, "aggregate", "member_count"
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "net new"
    assert "declares no net metric" in res.clarification.question


# ── a ratio needs a period ──────────────────────────────────────────────────


def test_a_ratio_over_no_period_reads_the_last_complete_month() -> None:
    model = _model(_DAY_PASS, _SHARE)
    res = _resolve("What is the day pass share?", model, "aggregate", "day_pass_share")
    assert res.intent is not None, res.clarification
    assert [(f.field, f.op, f.value) for f in res.intent.filters] == [
        ("visited_on", ">=", "2026-09-01"),
        ("visited_on", "<=", "2026-09-30"),
    ]
    pin = [d for d in res.decisions if d.provider == growth.PERIOD_DEFAULT]
    assert [(d.slot, d.chosen) for d in pin] == [("window", "2026-09-01/2026-09-30")]
    # September: 2 day passes of 4 visits
    assert _run(compile_intent(res.intent, model)) == [(0.5,)]
    line = growth.pin_disclosure(res.decisions, res.intent.metrics)
    assert line is not None and "September 2026" in line and "`day_pass_share`" in line
    # the sole-metric door reads it the same way
    sole = sole_metric_intent("day pass share", model)
    assert sole is not None
    pinned = pin_intent("day pass share", sole, model, TODAY)
    assert isinstance(pinned, tuple)
    assert pinned[0].filters == res.intent.filters and pinned[1] == pin[0]


@pytest.mark.parametrize(
    "question",
    [
        "What is the all-time day pass share?",
        "What was the day pass share in September 2026?",
        "Day pass share by month",
    ],
)
def test_a_ratio_with_a_period_or_all_time_acts(question: str) -> None:
    model = _model(_DAY_PASS, _SHARE)
    res = _resolve(question, model, "aggregate", "day_pass_share", "month")
    assert res.intent is not None, (question, res.clarification)
    assert res.intent.metrics == ["day_pass_share"]


# ── through the serving pipeline ────────────────────────────────────────────


def test_a_change_serves_through_the_pipeline_with_its_delta(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import duckdb

    from services.connectors.duckdb import DuckDBConnector
    from services.runtime.pipeline import run_query
    from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
    from tests.test_typed_question_shapes import DOMAINS, TODAY, _Prefers

    path = str(tmp_path / "gym.duckdb")
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE visits AS SELECT * FROM (VALUES "
        "('v1', 'm1', 'Aino', 'harbour wall', 'day pass', DATE '2026-09-03', 12.0),"
        "('v3', 'm2', 'Bruno', 'old mill', 'monthly pass', DATE '2026-09-11', 45.0),"
        "('v5', 'm2', 'Bruno', 'old mill', 'monthly pass', DATE '2026-08-14', 45.0),"
        "('v6', 'm3', 'Chidi', 'harbour wall', 'day pass', DATE '2026-08-21', 12.0),"
        "('v7', 'm3', 'Chidi', 'harbour wall', 'day pass', DATE '2026-08-22', 12.0)"
        ") AS t(visit_id, member_id, member_name, gym, pass_type, visited_on, fee)"
    )
    con.close()
    resolver = TypedResolver(
        _Prefers("aggregate", "fee_revenue"),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
    )
    res = run_query(
        question="MoM fee revenue growth in September 2026",
        lens_name="climbing",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=DuckDBConnector(path),
        generator=TypedIntentGenerator(resolver),
        composer=None,
    )
    assert res.response.status == "ok", res.response.answer
    data = res.response.data
    assert data is not None
    assert data.columns == [
        "fee_revenue",
        "fee_revenue_compare",
        "fee_revenue_delta",
        "fee_revenue_delta_pct",
    ]
    assert data.rows == [[57.0, 69.0, -12.0, pytest.approx(-12 / 69)]]


# ── which words ask, and which period comes first ───────────────────────────


@pytest.mark.parametrize(
    ("question", "to"),
    [
        ("Fee revenue in Q3 2025 vs Q3 2026", "2025-07-01/2025-09-30"),
        ("Fee revenue in Q3 2026 vs Q3 2025", "2025-07-01/2025-09-30"),
        ("Fee revenue last month vs the month before", "previous_period"),
        ("Fee revenue last month vs the previous month", "previous_period"),
        ("Did fee revenue drop last month?", "previous_period"),
        ("The change in fee revenue in September 2026", "previous_period"),
    ],
)
def test_the_change_runs_from_the_earlier_period(question: str, to: str) -> None:
    res = _fee(question)
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None and res.intent.compare.to == to, question


def test_a_verb_of_another_noun_is_not_a_change_of_the_figure() -> None:
    """Members who changed plan is an event to count, not a delta."""
    res = _resolve(
        "How many visits came from members who changed their pass last month?",
        _model(),
        "aggregate",
        "visit_count",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.compare is None


# ── a period binding is the window ──────────────────────────────────────────


def test_a_period_bound_after_a_clarification_is_the_window() -> None:
    """The change clarified for a period; re-asked with it bound, it compares."""
    res = _resolve(
        "What is our fee revenue growth?",
        _model(),
        "aggregate",
        "fee_revenue",
        bindings={"window": "2026-09"},
    )
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None and res.intent.compare.to == "previous_period"
    assert [(f.op, f.value) for f in res.intent.filters] == [
        (">=", "2026-09-01"),
        ("<=", "2026-09-30"),
    ]
    model = _model(_DAY_PASS, _SHARE)
    res = _resolve(
        "What is the day pass share?",
        model,
        "aggregate",
        "day_pass_share",
        bindings={"visited_on": "2026-09-01/2026-09-15"},
    )
    assert res.intent is not None, res.clarification
    assert [f.value for f in res.intent.filters] == ["2026-09-01", "2026-09-15"]


def test_a_period_binding_dst_cannot_read_clarifies() -> None:
    res = _resolve(
        "What is our fee revenue growth?",
        _model(),
        "aggregate",
        "fee_revenue",
        bindings={"window": "last autumn"},
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "window"


def test_the_generated_intent_path_pins_a_ratios_period_too() -> None:
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.intent_generator import IntentSQLGenerator

    model = _model(_DAY_PASS, _SHARE)
    bare = IntentSQLGenerator(ScriptedLLM(['{"metrics": ["day_pass_share"]}'])).generate(
        question="What is the day pass share?",
        semantic_model=model,
        prose_context=[],
        dialect="duckdb",
    )
    assert bare.sql and bare.clarification is None and bare.intent is not None
    assert len(bare.intent.filters) == 2
    assert [d.provider for d in bare.decisions] == [growth.PERIOD_DEFAULT]
    windowed = IntentSQLGenerator(
        ScriptedLLM(
            [
                '{"metrics": ["day_pass_share"], "filters": ['
                '{"field": "visited_on", "op": ">=", "value": "2026-09-01"}, '
                '{"field": "visited_on", "op": "<", "value": "2026-10-01"}]}'
            ]
        )
    ).generate(
        question="What was the day pass share in September 2026?",
        semantic_model=model,
        prose_context=[],
        dialect="duckdb",
    )
    assert windowed.sql and windowed.clarification is None
