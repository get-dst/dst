"""The cube lane's checks, one test per check, each naming the failure mode it
carries. The checks are pure functions of the ask and the Cube queries; the
few that look at data get a scripted ``run``."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from services.contracts.cube_probe import CubeProbe
from services.contracts.cube_query import Binding, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.cube.lens_view import CubeLensView
from services.verify import post, pre
from services.verify.base import Ask, Rows
from tests import _cube_fake as fake


def q(**body: Any) -> CubeQuery:
    return CubeQuery.model_validate(body)


def ask(
    question: str,
    queries: list[CubeQuery],
    *,
    ledger: list[tuple[str, str, str]] | None = None,
    view: CubeLensView | None = None,
    run: Any = None,
    groups: list[str] | None = None,
    lens: str = "shop",
    today: date | None = fake.TODAY,
) -> Ask:
    return Ask(
        question=question,
        lens=view or fake.view(),
        config=LensConfig(name=lens),
        groups=groups or [],
        ledger=None
        if ledger is None
        else [Binding(word=w, kind=k, target=t, source="test") for w, k, t in ledger],  # type: ignore[arg-type]
        asked=queries,
        run=run,
        today=today,
    )


AUG = {"dimension": "orders.ordered_at", "dateRange": ["2026-08-01", "2026-08-31"]}


# ── pre-execution ───────────────────────────────────────────────────────────


def test_scope_violation_refuses_a_member_outside_the_lens_views() -> None:
    v = fake.view(["shop"])
    inside = q(measures=["shop.revenue"])
    outside = q(measures=["quotes.won"])
    assert pre.scope_violation(ask("revenue", [inside], view=v), [inside]).outcome == "pass"
    c = pre.scope_violation(ask("quotes won", [outside], view=v), [outside])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.scope_violation)
    # No views declared: nothing to check against, said so, never a clean pass.
    assert pre.scope_violation(ask("x", [outside]), [outside]).outcome == "skip"


def test_hidden_column_refuses_a_member_withheld_from_the_caller() -> None:
    query = q(measures=["staff.headcount"], dimensions=["staff.salary"])
    c = pre.hidden_column(ask("headcount by salary", [query]), [query])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.hidden_column)
    assert pre.hidden_column(ask("x", [query], groups=["hr"]), [query]).outcome == "pass"


def test_not_declared_refuses_a_member_the_model_does_not_hold() -> None:
    query = q(measures=["orders.margin"])
    c = pre.not_declared(ask("margin", [query]), [query])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.not_declared)
    assert "orders.margin" in c.detail


def test_not_computable_refuses_a_declared_measure_by_name_or_alias() -> None:
    c = pre.not_computable(ask("what is our customer LTV this year", []), [])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.not_declared)
    assert "lifetime value" in c.detail
    assert pre.not_computable(ask("revenue in August", []), []).outcome == "pass"


def test_not_for_refuses_an_ask_another_lens_answers() -> None:
    c = pre.not_for(ask("contract churn last quarter", []), [])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.misrouted)
    assert "service" in c.detail


def _readings_run(net: float, by_refund: float) -> Any:
    def run(query: CubeQuery) -> Rows:
        m = query.measures[0]
        return [{m: net if m == "orders.revenue_net" else by_refund}]

    return run


def test_ambiguous_metric_serves_the_default_reading_when_the_readings_are_close() -> None:
    query = q(measures=["orders.revenue_net"], timeDimensions=[AUG])
    a = ask("webshop revenue in August", [query], run=_readings_run(1000.0, 1004.0))
    c = pre.ambiguous_metric(a, [query])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.ambiguous_metric)
    assert c.queries is not None
    assert c.queries[0].measures == ["refund_events.revenue_refund_date"]
    # The clock follows the counterpart's cube.
    assert c.queries[0].timeDimensions[0].dimension == "refund_events.event_at"
    assert "order date" in c.detail and "1000" in c.detail


def test_ambiguous_metric_asks_when_the_readings_differ() -> None:
    query = q(measures=["orders.revenue_net"], timeDimensions=[AUG])
    a = ask("webshop revenue in August", [query], run=_readings_run(1000.0, 1100.0))
    c = pre.ambiguous_metric(a, [query])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.ambiguous_metric)
    assert "differ by up to" in c.detail
    # A question that names the reading is served as asked.
    named = ask("revenue by order date in August", [query], run=_readings_run(1.0, 9.0))
    assert pre.ambiguous_metric(named, [query]).outcome == "pass"


def _refund_term(default: str | None = None) -> list[dict[str, Any]]:
    """The fake lens's refund-basis term with no reading-level default, and
    the term-level declared ``default`` when given."""
    return [
        {
            "metric": "refund_basis",
            "summary": "Ambiguous - revenue net of refunds by order date or by refund date.",
            **({"default": default} if default else {}),
            "readings": [
                {
                    "says": "refunds netted against their orders (order date)",
                    "members": ["orders.revenue_net"],
                    "select_words": ["order date"],
                },
                {
                    "says": "refunds netted when issued (refund date)",
                    "members": ["refund_events.revenue_refund_date"],
                    "select_words": ["refund date"],
                },
            ],
        }
    ]


BY_REFUND = "refunds netted when issued (refund date)"


def test_a_declared_default_reading_is_served_with_the_others_disclosed() -> None:
    """A term with a declared `default:` serves it however far apart the
    readings are, and discloses the others with their values; asking is
    left to terms with no default."""
    view = fake.view(ambiguous=_refund_term(BY_REFUND))
    query = q(measures=["orders.revenue_net"], timeDimensions=[AUG])
    a = ask("webshop revenue in August", [query], view=view, run=_readings_run(1000.0, 1100.0))
    c = pre.ambiguous_metric(a, [query])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.ambiguous_metric)
    assert c.queries is not None
    assert c.queries[0].measures == ["refund_events.revenue_refund_date"]
    assert "declared default" in c.detail and "1000" in c.detail and "order date" in c.detail
    # with no executor to compute the others, they are still named, never asked
    blind = ask("webshop revenue in August", [query], view=view)
    c2 = pre.ambiguous_metric(blind, [query])
    assert c2.outcome == "disclose" and "order date" in c2.detail
    assert c2.queries is not None
    assert c2.queries[0].measures == ["refund_events.revenue_refund_date"]


def test_with_no_default_close_readings_serve_and_far_ones_ask() -> None:
    view = fake.view(ambiguous=_refund_term())
    query = q(measures=["orders.revenue_net"], timeDimensions=[AUG])
    close = ask("webshop revenue in August", [query], view=view, run=_readings_run(1000.0, 1004.0))
    c = pre.ambiguous_metric(close, [query])
    assert c.outcome == "disclose" and c.queries is not None
    assert c.queries[0].measures == ["orders.revenue_net"]  # served as read
    far = ask("webshop revenue in August", [query], view=view, run=_readings_run(1000.0, 1100.0))
    assert pre.ambiguous_metric(far, [query]).outcome == "clarify"


def test_a_declared_default_must_name_one_of_the_terms_readings() -> None:
    from services.contracts.lens_model import Overlay

    with pytest.raises(ValueError, match="default"):
        Overlay.model_validate({"ambiguous": _refund_term("refunds by the phase of the moon")})
    term = _refund_term(BY_REFUND)
    term[0]["readings"][0]["default"] = True  # a second default, on another reading
    with pytest.raises(ValueError, match="default"):
        Overlay.model_validate({"ambiguous": term})


# A line-level refund rate counted on its order's date (a clock on another
# cube, reached through the join) and its refund-date counterpart on an event
# stream with its own clock; categories on a product cube both reach.
def _lines_view(
    *, events_join_products: bool = True, events_clock: bool = True, **caveats: Any
) -> CubeLensView:
    from services.contracts.cube_meta import MetaDoc
    from services.contracts.cube_probe import CubeProbe
    from services.contracts.lens_model import Overlay

    def m(name: str, **dst: Any) -> dict[str, Any]:
        return {"name": name, "type": "number", "meta": {"dst": dst} if dst else {}}

    def d(name: str, kind: str = "string") -> dict[str, Any]:
        return {"name": name, "type": kind}

    def j(*names: str) -> list[dict[str, Any]]:
        return [{"name": n, "relationship": "many_to_one"} for n in names]

    payload = {
        "cubes": [
            {
                "name": "orders",
                "measures": [m("orders.count", clock="ordered_at")],
                "dimensions": [d("orders.order_id"), d("orders.ordered_at", "time")],
            },
            {"name": "products", "measures": [], "dimensions": [d("products.category")]},
            {
                "name": "lines",
                "measures": [m("lines.refund_rate", clock="orders.ordered_at")],
                "dimensions": [d("lines.order_id"), d("lines.product_id")],
                "joins": j("orders", "products"),
            },
            {
                "name": "line_events",
                "measures": [
                    m(
                        "line_events.refund_rate_issued",
                        **({"clock": "event_at"} if events_clock else {}),
                    )
                ],
                "dimensions": [d("line_events.event_at", "time"), d("line_events.product_id")],
                "joins": j("orders", *(["products"] if events_join_products else [])),
            },
        ]
    }
    overlay = {
        "ambiguous": [
            {
                "metric": "refund_basis",
                "summary": "Ambiguous - refunds against their own orders or when issued.",
                "default": "refunds when issued (refund date)",
                "readings": [
                    {
                        "says": "refunds against their own orders (order date)",
                        "members": ["lines.refund_rate"],
                        "select_words": ["order date"],
                    },
                    {
                        "says": "refunds when issued (refund date)",
                        "members": ["line_events.refund_rate_issued"],
                        "select_words": ["refund date"],
                    },
                ],
            }
        ],
        "cubes": {
            cube: {"member_caveats": {member: [text]}}
            for key, text in caveats.items()
            for cube, member in [key.split("__")]
        },
    }
    return CubeLensView.build(
        MetaDoc.parse(payload),
        overlay=Overlay.model_validate(overlay),
        probe=CubeProbe.model_validate({"clock_end": "2026-08-31"}),
    )


BY_CATEGORY_IN_2026 = {
    "measures": ["lines.refund_rate"],
    "dimensions": ["products.category"],
    "timeDimensions": [
        {"dimension": "orders.ordered_at", "dateRange": ["2026-01-01", "2026-08-31"]}
    ],
}


def _by_category(query: CubeQuery) -> Rows:
    m = query.measures[0]
    return [
        {"products.category": "Valves", m: 0.0287 if m == "lines.refund_rate" else 0.0301},
        {"products.category": "Pumps", m: 0.0189 if m == "lines.refund_rate" else 0.0170},
    ]


def test_a_default_reading_on_another_cube_is_windowed_on_its_own_clock() -> None:
    """The asked measure's clock lives on a joined cube (the order date);
    switching to the declared default's counterpart maps the window to the
    counterpart's own clock, so the served query is never windowed off it."""
    view = _lines_view()
    query = q(**BY_CATEGORY_IN_2026)
    a = ask("refund rate by category this year", [query], view=view, run=_by_category)
    c = pre.ambiguous_metric(a, [query])
    assert c.outcome == "disclose", c.detail
    assert c.queries is not None
    (served,) = c.queries
    assert served.measures == ["line_events.refund_rate_issued"]
    assert [t.dimension for t in served.timeDimensions] == ["line_events.event_at"]
    assert served.timeDimensions[0].dateRange == ["2026-01-01", "2026-08-31"]
    assert served.dimensions == ["products.category"]
    assert pre.window_misbound(a, c.queries).outcome != "clarify"


def test_a_default_reading_that_cannot_read_the_window_is_asked_by_name() -> None:
    """No clock on the counterpart, or no join path from it to a member the
    query keeps: the caller is asked, naming why, never served a mismatch."""
    query = q(**BY_CATEGORY_IN_2026)
    for view, why in [
        (_lines_view(events_clock=False), "no clock"),
        (_lines_view(events_join_products=False), "no join path"),
    ]:
        a = ask("refund rate by category this year", [query], view=view, run=_by_category)
        c = pre.ambiguous_metric(a, [query])
        assert c.outcome == "clarify", c.detail
        assert why in c.detail and "refunds when issued" in c.detail


def test_a_disclosed_reading_s_figures_carry_their_caveats() -> None:
    """The other reading's figures printed beside the default carry that
    member's caveats (an allocation), so wherever the disclosure travels —
    an answer or a clarification — the figure is never bare."""
    allocated = "allocated: refunds spread over lines by each line's share"
    view = _lines_view(lines__refund_rate=allocated)
    query = q(**BY_CATEGORY_IN_2026)
    a = ask("refund rate by category this year", [query], view=view, run=_by_category)
    c = pre.ambiguous_metric(a, [query])
    assert c.outcome == "disclose" and "0.0287" in c.detail
    assert allocated in c.detail


def test_uncovered_word_asks_for_a_word_nothing_in_the_query_carries() -> None:
    query = q(measures=["orders.count"], timeDimensions=[AUG])
    led = [("orders", "member", "orders.count"), ("August", "period", "2026-08")]
    ok = pre.uncovered_word(ask("orders in August", [query], ledger=led), [query])
    assert ok.outcome == "pass"
    c = pre.uncovered_word(ask("cancelled orders in August", [query], ledger=led), [query])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.uncovered_word)
    assert "'cancelled'" in c.detail
    # No ledger: fails loud, never a pass on nothing.
    none = pre.uncovered_word(ask("orders in August", [query]), [query])
    assert none.outcome == "clarify"


def test_uncovered_word_binds_a_company_word_structurally() -> None:
    query = q(measures=["orders.revenue"], timeDimensions=[AUG])
    led = [("revenue", "member", "orders.revenue"), ("north", "value", "North Oy")]
    c = pre.uncovered_word(ask("north revenue in August", [query], ledger=led), [query])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.uncovered_word)
    assert "scope 'north'" in c.detail
    bound = q(
        measures=["orders.revenue"],
        timeDimensions=[AUG],
        filters=[{"member": "orders.company", "operator": "equals", "values": ["North Oy"]}],
    )
    assert (
        pre.uncovered_word(ask("north revenue in August", [bound], ledger=led), [bound]).outcome
        == "pass"
    )


def _by_company(query: CubeQuery) -> Rows:
    return [
        {"orders.company": "North Oy", query.measures[0]: 400.0},
        {"orders.company": "South AB", query.measures[0]: 570.0},
    ]


def test_unasked_filter_asks_about_a_company_the_question_does_not_name() -> None:
    scoped = q(
        measures=["orders.revenue"],
        timeDimensions=[AUG],
        filters=[{"member": "orders.company", "operator": "equals", "values": ["North Oy"]}],
    )
    c = pre.unasked_filter(ask("revenue in August", [scoped], run=_by_company), [scoped])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.unasked_filter)
    assert "South AB" in c.detail
    # A region of the company's own CRM names it (data, not code).
    named = ask("revenue in Uusimaa in August", [scoped], run=_by_company)
    assert pre.unasked_filter(named, [scoped]).outcome == "pass"
    # A single-company cube with a declared group-level member.
    fi = q(measures=["north_invoices.invoiced"])
    c2 = pre.unasked_filter(ask("total invoiced revenue", [fi]), [fi])
    assert c2.outcome == "clarify" and "orders.revenue" in c2.detail


def test_unbound_value_rewrites_a_unique_spelling_and_asks_for_an_unknown_one() -> None:
    alias = q(
        measures=["orders.count"],
        filters=[{"member": "orders.status", "operator": "equals", "values": ["settled"]}],
    )
    c = pre.unbound_value(ask("settled orders", [alias]), [alias])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.unbound_value)
    assert c.queries is not None and c.queries[0].filters[0].values == ["Paid"]
    unknown = q(
        measures=["orders.count"],
        filters=[{"member": "orders.status", "operator": "equals", "values": ["Cancelled"]}],
    )
    c2 = pre.unbound_value(ask("cancelled orders", [unknown]), [unknown])
    assert (c2.outcome, c2.mode) == ("clarify", FailureMode.unbound_value)


def test_window_misbound_asks_when_a_measure_is_windowed_off_its_clock() -> None:
    sent = q(
        measures=["quotes.won"],
        timeDimensions=[{"dimension": "quotes.sent_at", "dateRange": ["2026-07-01", "2026-09-30"]}],
    )
    c = pre.window_misbound(ask("quotes won this quarter", [sent]), [sent])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.window_misbound)


def test_window_misbound_clips_a_window_to_the_lens_clock() -> None:
    decided = q(
        measures=["quotes.won"],
        timeDimensions=[
            {"dimension": "quotes.decided_at", "dateRange": ["2026-07-01", "2026-09-30"]}
        ],
    )
    c = pre.window_misbound(ask("quotes won this quarter", [decided]), [decided])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.window_misbound)
    assert c.queries is not None
    assert c.queries[0].timeDimensions[0].dateRange == ["2026-07-01", "2026-08-31"]
    # A window that starts after the clock is left as asked: nothing of it is
    # loaded, and the freshness check says so instead of serving the clock's day.
    later = decided.model_copy(deep=True)
    later.timeDimensions[0].dateRange = ["2026-09-01", "2026-09-30"]
    c2 = pre.window_misbound(ask("quotes won in September", [later]), [later])
    assert c2.outcome == "pass"


def _week(start: str, end: str) -> dict[str, Any]:
    return {"dimension": "orders.ordered_at", "dateRange": [start, end]}


def test_a_relative_period_is_bound_to_the_window_dst_resolved() -> None:
    # Tuesday 2026-09-01: "this week" is the ISO week from Monday 08-31.
    shifted = q(measures=["orders.count"], timeDimensions=[_week("2026-08-24", "2026-08-30")])
    c = pre.window_misbound(ask("orders this week", [shifted]), [shifted])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.window_misbound)
    assert c.queries is not None
    assert c.queries[0].timeDimensions[0].dateRange == ["2026-08-31", "2026-09-06"]
    assert "'this week' is 2026-08-31..2026-09-06" in c.detail
    assert "partial" in c.detail and "Monday" in c.detail


def test_each_period_of_a_comparison_is_bound_to_its_own_window() -> None:
    both = q(
        measures=["orders.count"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-08-24", "2026-08-30"], ["2026-08-17", "2026-08-23"]],
            }
        ],
    )
    c = pre.window_misbound(ask("orders this week vs last week", [both]), [both])
    assert c.queries is not None
    assert c.queries[0].timeDimensions[0].compareDateRange == [
        ["2026-08-31", "2026-09-06"],
        ["2026-08-24", "2026-08-30"],
    ]


def test_a_window_that_agrees_with_the_period_is_kept_and_the_partial_week_said() -> None:
    to_date = q(measures=["orders.count"], timeDimensions=[_week("2026-08-31", "2026-09-01")])
    c = pre.window_misbound(ask("orders this week", [to_date]), [to_date])
    assert c.outcome == "disclose" and c.queries is not None
    assert c.queries[0].timeDimensions[0].dateRange == ["2026-08-31", "2026-09-01"]
    last = q(measures=["orders.count"], timeDimensions=[_week("2026-08-24", "2026-08-30")])
    assert pre.window_misbound(ask("orders last week", [last]), [last]).outcome == "pass"


def test_a_period_the_windows_cannot_be_reconciled_with_is_asked() -> None:
    two = q(
        measures=["orders.count"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-08-10", "2026-08-16"], ["2026-08-17", "2026-08-23"]],
            }
        ],
    )
    c = pre.window_misbound(ask("orders this week", [two]), [two])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.window_misbound)
    assert "this week" in c.detail


def test_a_past_period_read_to_the_same_point_as_a_to_date_one_is_kept() -> None:
    like = q(
        measures=["orders.count"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-08-31", "2026-09-01"], ["2026-08-24", "2026-08-25"]],
            }
        ],
    )
    c = pre.window_misbound(ask("orders this week vs last week", [like]), [like])
    assert c.queries is not None
    assert c.queries[0].timeDimensions[0].compareDateRange == [
        ["2026-08-31", "2026-09-01"],
        ["2026-08-24", "2026-08-25"],
    ]


@pytest.mark.parametrize(
    "question",
    [
        "orders this month compared to the same month last year",
        "orders in August this year",
        "how did orders change compared to this time last year",
    ],
)
def test_a_relative_phrase_that_modifies_another_period_is_left_to_the_query(
    question: str,
) -> None:
    # "last year" here names the year of another period, not a window of its
    # own: binding it would be a guess, so nothing is rewritten.
    sep = q(measures=["orders.count"], timeDimensions=[_week("2025-09-01", "2025-09-30")])
    assert pre.window_misbound(ask(question, [sep]), [sep]).queries is None


def test_label_key_groups_a_name_with_its_key() -> None:
    query = q(measures=["orders.revenue"], dimensions=["orders.customer_name"])
    c = pre.label_key(ask("revenue by customer", [query]), [query])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.wrong_grain)
    assert c.queries is not None
    assert c.queries[0].dimensions == ["orders.customer_id", "orders.customer_name"]


def test_the_pre_checks_run_in_the_fixed_order() -> None:
    assert [f.__name__ for f in pre.PRE_ORDER] == [
        "not_computable",
        "not_for",
        "scope_violation",
        "hidden_column",
        "not_declared",
        "ambiguous_metric",
        "uncovered_word",
        "unasked_filter",
        "unbound_value",
        "window_misbound",
        "label_key",
    ]


# ── on what the data holds ──────────────────────────────────────────────────


def test_data_gap_discloses_a_feed_that_stops_inside_the_window() -> None:
    query = q(measures=["orders.revenue"], timeDimensions=[AUG])
    c = post.data_gap(ask("revenue in August", [query]), query)
    assert (c.outcome, c.mode) == ("disclose", FailureMode.data_gap)
    assert "company=North Oy" in c.detail and "2026-06-05" in c.detail
    # Narrowed to the live feed: nothing to disclose.
    south = query.model_copy(
        update={
            "filters": [
                *query.filters,
                *q(
                    filters=[
                        {"member": "orders.company", "operator": "equals", "values": ["South AB"]}
                    ]
                ).filters,
            ]
        }
    )
    assert post.data_gap(ask("south revenue in August", [south]), south).outcome == "pass"


def test_data_gap_refuses_a_window_with_nothing_loaded() -> None:
    query = q(
        measures=["orders.revenue"],
        timeDimensions=[
            {"dimension": "orders.ordered_at", "dateRange": ["2026-09-01", "2026-09-30"]}
        ],
    )
    c = post.data_gap(ask("revenue in September", [query]), query)
    assert (c.outcome, c.mode) == ("refuse", FailureMode.data_gap)


def test_partial_period_discloses_a_comparison_against_a_partial_month() -> None:
    """A partial period is a fact dst owns and a framing the consumer owns:
    the figure is served, never refused, with which period, how complete,
    judged by which measure, and that it is set against complete ones."""
    aug = q(measures=["orders.count"], timeDimensions=[AUG])
    jul = q(
        measures=["orders.count"],
        timeDimensions=[
            {"dimension": "orders.ordered_at", "dateRange": ["2026-07-01", "2026-07-31"]}
        ],
    )
    c = post.partial_period(ask("orders August vs July", [aug, jul]), [aug, jul])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.data_gap)
    assert "2026-08" in c.detail and "40%" in c.detail and "judged by orders.count" in c.detail
    assert "against complete periods" in c.detail
    one = post.partial_period(ask("orders in August", [aug]), [aug])
    assert (one.outcome, one.mode) == ("disclose", FailureMode.data_gap)
    assert "against complete periods" not in one.detail
    # the monitor's evidence beyond volume rides the disclosure
    import copy

    probe = copy.deepcopy(fake.PROBE)
    probe["measures"]["orders.count"]["partial"]["evidence"] = "no rows on the period's last 7 days"
    view = CubeLensView.build(fake.doc(), overlay=fake.view().overlay, probe=CubeProbe(**probe))
    told = post.partial_period(ask("orders in August", [aug], view=view), [aug])
    assert "no rows on the period's last 7 days" in told.detail


def test_empty_result_says_nothing_was_recorded_instead_of_zero() -> None:
    query = q(
        measures=["orders.count"],
        filters=[{"member": "orders.status", "operator": "equals", "values": ["Open"]}],
    )
    c = post.empty_result(ask("open orders", [query]), query, [{"orders.count": 0}])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.empty_result)
    assert post.empty_result(ask("x", [query]), query, [{"orders.count": 3}]).outcome == "pass"


def test_null_bucket_is_never_ranked_first() -> None:
    query = q(
        measures=["orders.count"],
        dimensions=["orders.customer_id"],
        order={"orders.count": "desc"},
        limit=2,
    )
    rows = [
        {"orders.customer_id": None, "orders.count": 9},
        {"orders.customer_id": "C1", "orders.count": 2},
    ]

    def run(qq: CubeQuery) -> Rows:
        assert any(f.operator == "set" for f in qq.filters)
        return [
            {"orders.customer_id": "C1", "orders.count": 2},
            {"orders.customer_id": "C2", "orders.count": 1},
        ]

    c = post.null_bucket(ask("top customers", [query], run=run), query, rows)
    assert (c.outcome, c.mode) == ("disclose", FailureMode.wrong_grain)
    assert c.rows is not None and c.rows[0]["orders.customer_id"] == "C1"
    assert "orders.count=9" in c.detail


def test_tie_at_limit_extends_the_rows_that_tie_the_last_one() -> None:
    query = q(
        measures=["orders.count"],
        dimensions=["orders.customer_id"],
        order=[["orders.count", "desc"], ["orders.customer_id", "asc"]],
        limit=2,
    )
    ext = [
        {"orders.customer_id": "C1", "orders.count": 5},
        {"orders.customer_id": "C2", "orders.count": 3},
        {"orders.customer_id": "C3", "orders.count": 3},
        {"orders.customer_id": "C4", "orders.count": 1},
    ]
    c = post.tie_at_limit(ask("top 2", [query], run=lambda qq: ext), query, ext[:2])
    assert (c.outcome, c.mode) == ("disclose", FailureMode.partial_ask)
    assert c.rows == ext[:3]


def test_snapshot_pin_reads_a_level_at_its_grain_and_keeps_the_latest() -> None:
    v = fake.view()
    query = q(
        measures=["staff.headcount"],
        dimensions=["staff.company"],
        timeDimensions=[{"dimension": "staff.month", "dateRange": ["2026-04-01", "2026-06-30"]}],
    )
    parts = post.snapshot_split(v, query)
    assert len(parts) == 1
    pinned, pin, coarse = parts[0]
    assert pin == "staff.month.month" and pinned.timeDimensions[0].granularity == "month"
    rows = [
        {"staff.company": "North Oy", "staff.month.month": f"2026-0{m}-01", "staff.headcount": h}
        for m, h in ((4, 40), (5, 42), (6, 43))
    ]
    assert post.snapshot_reduce(pinned, rows, pin, coarse) == [rows[-1]]
    c = post.snapshot_pin(ask("headcount at the end of Q2", [query]), query)
    assert (c.outcome, c.mode) == ("disclose", FailureMode.wrong_grain)


def test_compare_pivot_sets_each_window_side_by_side() -> None:
    query = q(
        measures=["orders.count"],
        dimensions=["orders.status"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-08-01", "2026-08-31"], ["2026-07-01", "2026-07-31"]],
            }
        ],
    )
    rows = [
        {"orders.status": "Paid", "orders.count": 5, "compareDateRange": "2026-08-01 - 2026-08-31"},
        {
            "orders.status": "Refunded",
            "orders.count": 1,
            "compareDateRange": "2026-08-01 - 2026-08-31",
        },
        {"orders.status": "Paid", "orders.count": 1, "compareDateRange": "2026-07-01 - 2026-07-31"},
    ]
    out = post.compare_pivot(query, rows)
    refunded = next(r for r in out if r["orders.status"] == "Refunded")
    assert refunded["orders.count (2026-08-01 - 2026-08-31)"] == 1
    assert refunded["orders.count (2026-07-01 - 2026-07-31)"] == 0


@pytest.mark.parametrize("check", [*pre.PRE_ORDER])
def test_every_pre_check_returns_one_named_mode(check: Any) -> None:
    query = q(measures=["orders.count"], timeDimensions=[AUG])
    c = check(
        ask("orders in August", [query], ledger=[("orders", "member", "orders.count")]), [query]
    )
    assert c.mode is not None and c.name


def test_empty_window_names_the_side_of_a_comparison_with_nothing_recorded() -> None:
    query = q(
        measures=["orders.count"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-07-01", "2026-09-30"], ["2026-04-01", "2026-06-30"]],
            }
        ],
    )
    rows = [
        {"orders.count": None, "compareDateRange": "2026-07-01 - 2026-09-30"},
        {"orders.count": 4, "compareDateRange": "2026-04-01 - 2026-06-30"},
    ]
    c = post.empty_window(ask("orders this quarter vs last", [query]), query, rows)
    assert (c.outcome, c.mode) == ("disclose", FailureMode.data_gap)
    assert "2026-07-01 - 2026-09-30" in c.detail
    full = [{**rows[0], "orders.count": 2}, rows[1]]
    assert post.empty_window(ask("x", [query]), query, full).outcome == "pass"


def test_hidden_column_refuses_a_member_a_view_re_exposes() -> None:
    """A view that includes a hidden dimension (Cube serves it as public)
    is still refused on dst's side: the rule follows the cube member."""
    from services.contracts.cube_meta import MetaDoc
    from services.cube.lens_view import CubeLensView

    meta = fake.meta_payload()
    meta["cubes"].append(
        {
            "name": "people",
            "type": "view",
            "measures": [],
            "dimensions": [
                {"name": "people.salary", "type": "string", "aliasMember": "staff.salary"}
            ],
            "segments": [],
        }
    )
    view = CubeLensView(doc=MetaDoc.parse(meta), overlay=fake.view().overlay)
    query = q(dimensions=["people.salary"])
    c = pre.hidden_column(ask("salaries", [query], view=view), [query])
    assert (c.outcome, c.mode) == ("refuse", FailureMode.hidden_column)


def test_hidden_column_refuses_a_member_cube_withheld_from_the_caller_s_meta() -> None:
    """A view's access policy drops the member from the caller's /v1/meta
    altogether; asked by its view name it is refused as hidden, not as a
    member the model does not hold."""
    from services.contracts.cube_meta import MetaDoc
    from services.cube.lens_view import CubeLensView

    meta = fake.meta_payload()
    meta["cubes"].append(
        {"name": "people", "type": "view", "measures": [], "dimensions": [], "segments": []}
    )
    view = CubeLensView(doc=MetaDoc.parse(meta), overlay=fake.view().overlay)
    for member in ("people.staff_salary", "people.salary"):
        query = q(dimensions=[member])
        c = pre.hidden_column(ask("salaries", [query], view=view), [query])
        assert (c.outcome, c.mode) == ("refuse", FailureMode.hidden_column)
        granted = pre.hidden_column(ask("salaries", [query], view=view, groups=["hr"]), [query])
        assert granted.outcome == "pass"
    query = q(dimensions=["people.bonus"])
    assert pre.hidden_column(ask("bonus", [query], view=view), [query]).outcome == "pass"
    assert pre.not_declared(ask("bonus", [query], view=view), [query]).outcome == "refuse"
