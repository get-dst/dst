"""CubeQuery: the identity of an ask (canonical form and sha256), and the
translation to and from today's QueryIntent, member for member."""

from __future__ import annotations

import itertools
import random
from datetime import date

import pytest
from pydantic import ValidationError

from services.contracts.cube_query import (
    DEFAULT_LIMIT,
    Binding,
    Candidate,
    CubeFilter,
    CubeQuery,
    TimeDimension,
    UnknownMember,
    ViewMembers,
    absolute_range,
    from_intent,
    to_intent,
)
from services.contracts.query_intent import IntentCompare, IntentFilter, IntentOrder, QueryIntent

TODAY = date(2026, 10, 29)

VIEW = ViewMembers(
    view="orders_view",
    measures=["orders_view.revenue", "orders_view.count"],
    dimensions=["orders_view.country", "orders_view.status", "orders_view.ordered_at"],
    segments=["orders_view.paid"],
    time_dimensions=["orders_view.ordered_at"],
    clock="orders_view.ordered_at",
)


def _q(**kw: object) -> CubeQuery:
    return CubeQuery.model_validate(kw)


# ── canonical form and hashing ────────────────────────────────────────────


def test_member_order_does_not_change_the_hash() -> None:
    a = _q(measures=["v.a", "v.b"], dimensions=["v.x", "v.y"], segments=["v.s", "v.t"])
    b = _q(measures=["v.b", "v.a"], dimensions=["v.y", "v.x"], segments=["v.t", "v.s"])
    assert a.sha256() == b.sha256()


def test_filter_order_and_value_order_do_not_change_the_hash() -> None:
    a = _q(
        filters=[
            {"member": "v.country", "operator": "equals", "values": ["FI", "SE"]},
            {"member": "v.status", "operator": "equals", "values": ["paid"]},
        ]
    )
    b = _q(
        filters=[
            {"member": "v.status", "operator": "equals", "values": ["paid"]},
            {"member": "v.country", "operator": "equals", "values": ["SE", "FI"]},
        ]
    )
    assert a.sha256() == b.sha256()


def test_nested_boolean_groups_canonicalise_too() -> None:
    a = _q(
        filters=[
            {
                "or": [
                    {"member": "v.a", "operator": "equals", "values": ["1"]},
                    {"member": "v.b", "operator": "gt", "values": ["2"]},
                ]
            }
        ]
    )
    b = _q(
        filters=[
            {
                "or": [
                    {"member": "v.b", "operator": "gt", "values": ["2"]},
                    {"member": "v.a", "operator": "equals", "values": ["1"]},
                ]
            }
        ]
    )
    assert a.sha256() == b.sha256()
    assert a.canonical()["filters"][0]["or"][0]["member"] == "v.a"


def test_the_defaults_cube_would_apply_are_written_out() -> None:
    c = _q(measures=["v.a"]).canonical()
    assert c["limit"] == DEFAULT_LIMIT and c["timezone"] == "UTC" and c["offset"] == 0
    assert c["order"] == [] and c["ungrouped"] is False and c["snapshot"] is None
    explicit = _q(measures=["v.a"], limit=DEFAULT_LIMIT, timezone="UTC")
    assert explicit.sha256() == _q(measures=["v.a"]).sha256()


def test_what_changes_the_ask_changes_the_hash() -> None:
    base = _q(measures=["v.a"])
    for other in (
        _q(measures=["v.a"], limit=10),
        _q(measures=["v.a"], timezone="Europe/Helsinki"),
        _q(measures=["v.a"], order=[("v.a", "desc")]),
        _q(measures=["v.a"], snapshot="last"),
        _q(measures=["v.a"], segments=["v.paid"]),
        _q(measures=["v.a"], ungrouped=True),
    ):
        assert other.sha256() != base.sha256()


def test_a_relative_window_resolves_to_absolute_days_against_the_clock() -> None:
    td = TimeDimension(dimension="v.t", dateRange="last month")
    assert td.canonical(TODAY)["dateRange"] == ["2026-09-01", "2026-09-30"]
    assert td.canonical(None)["dateRange"] == "last month", "no clock: kept as written"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("today", ["2026-10-29", "2026-10-29"]),
        ("yesterday", ["2026-10-28", "2026-10-28"]),
        ("last 7 days", ["2026-10-23", "2026-10-29"]),
        ("this month", ["2026-10-01", "2026-10-31"]),
        ("last quarter", ["2026-07-01", "2026-09-30"]),
        ("this year", ["2026-01-01", "2026-12-31"]),
        ("last year", ["2025-01-01", "2025-12-31"]),
        ("last 2 months", ["2026-08-01", "2026-09-30"]),
        ("this week", ["2026-10-26", "2026-11-01"]),
        ("2026-02", ["2026-02-01", "2026-02-28"]),
        ("2025", ["2025-01-01", "2025-12-31"]),
        ("2026-03-05", ["2026-03-05", "2026-03-05"]),
        ("from the dawn of time", "from the dawn of time"),
    ],
)
def test_absolute_range(phrase: str, expected: list[str] | str) -> None:
    assert absolute_range(phrase, TODAY) == expected


def test_a_pair_of_months_widens_to_days() -> None:
    assert absolute_range(["2026-01", "2026-02"], None) == ["2026-01-01", "2026-02-28"]


def test_same_intent_same_sha_whatever_order_a_lane_wrote_it_in() -> None:
    """The property behind certified pairs: one ask, one hash."""
    rng = random.Random(7)
    members = ["orders_view.revenue", "orders_view.count"]
    dims = ["orders_view.country", "orders_view.status"]
    filters = [
        IntentFilter(field="country", op="in", value=["FI", "SE"]),
        IntentFilter(field="status", op="=", value="paid"),
        IntentFilter(field="ordered_at", op=">=", value="2026-01-01"),
        IntentFilter(field="ordered_at", op="<=", value="2026-03-31"),
    ]
    hashes = set()
    for _ in range(20):
        rng.shuffle(members)
        rng.shuffle(dims)
        rng.shuffle(filters)
        intent = QueryIntent(
            entity="orders_view",
            metrics=[m.split(".")[1] for m in members],
            dimensions=[d.split(".")[1] for d in dims],
            filters=list(filters),
            grain="month",
        )
        hashes.add(from_intent(intent, VIEW).sha256(TODAY))
    assert len(hashes) == 1


def test_members_lists_every_name_once() -> None:
    q = _q(
        measures=["v.a"],
        dimensions=["v.x"],
        segments=["v.s"],
        timeDimensions=[{"dimension": "v.t"}],
        filters=[{"and": [{"member": "v.x", "operator": "equals", "values": ["1"]}]}],
        order=[("v.a", "desc")],
    )
    assert q.members() == ["v.a", "v.x", "v.s", "v.t"]


def test_filter_values_are_strings_on_the_wire() -> None:
    f = CubeFilter(member="v.flag", operator="equals", values=[True])  # type: ignore[list-item]
    assert f.wire() == {"member": "v.flag", "operator": "equals", "values": ["true"]}


# ── from_intent / to_intent ───────────────────────────────────────────────


def test_an_intent_becomes_a_query_over_its_view() -> None:
    intent = QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        dimensions=["country"],
        definitions=["paid"],
        grain="month",
        filters=[
            IntentFilter(field="status", op="=", value="paid"),
            IntentFilter(field="ordered_at", op=">=", value="2026-01-01"),
            IntentFilter(field="ordered_at", op="<", value="2026-04-01"),
        ],
        order_by=[IntentOrder(field="revenue", dir="desc")],
        limit=5,
    )
    q = from_intent(intent, VIEW)
    assert q.wire() == {
        "measures": ["orders_view.revenue"],
        "dimensions": ["orders_view.country"],
        "segments": ["orders_view.paid"],
        "timeDimensions": [
            {
                "dimension": "orders_view.ordered_at",
                "granularity": "month",
                "dateRange": ["2026-01-01", "2026-03-31"],
            }
        ],
        "filters": [{"member": "orders_view.status", "operator": "equals", "values": ["paid"]}],
        "order": [["orders_view.revenue", "desc"]],
        "limit": 5,
    }


def test_a_month_bound_on_the_clock_is_its_whole_month() -> None:
    intent = QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        filters=[IntentFilter(field="ordered_at", op="=", value="2026-02")],
    )
    assert from_intent(intent, VIEW).timeDimensions[0].dateRange == ["2026-02-01", "2026-02-28"]


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("=", "FI", ("equals", ["FI"])),
        ("!=", "FI", ("notEquals", ["FI"])),
        ("in", ["FI", "SE"], ("equals", ["FI", "SE"])),
        (">", 10, ("gt", ["10"])),
        (">=", 10, ("gte", ["10"])),
        ("<", 10, ("lt", ["10"])),
        ("<=", 10, ("lte", ["10"])),
        ("like", "nor", ("contains", ["nor"])),
    ],
)
def test_every_intent_operator_has_a_cube_operator(
    op: str, value: object, expected: tuple[str, list[str]]
) -> None:
    intent = QueryIntent(
        entity="orders_view",
        metrics=["count"],
        filters=[IntentFilter(field="country", op=op, value=value)],
    )  # type: ignore[arg-type]
    (f,) = from_intent(intent, VIEW).filters
    assert (f.operator, f.values) == expected


def test_compare_becomes_compare_date_range_with_the_ask_first() -> None:
    window = [
        IntentFilter(field="ordered_at", op=">=", value="2026-02-01"),
        IntentFilter(field="ordered_at", op="<=", value="2026-02-28"),
    ]
    previous = from_intent(
        QueryIntent(
            entity="orders_view",
            metrics=["revenue"],
            filters=window,
            compare=IntentCompare(to="previous_period"),
        ),
        VIEW,
    )
    assert previous.timeDimensions[0].compareDateRange == [
        ["2026-02-01", "2026-02-28"],
        ["2026-01-04", "2026-01-31"],
    ]
    assert previous.timeDimensions[0].dateRange is None
    year = from_intent(
        QueryIntent(
            entity="orders_view",
            metrics=["revenue"],
            filters=window,
            compare=IntentCompare(to="previous_year"),
        ),
        VIEW,
    )
    assert year.timeDimensions[0].compareDateRange == [
        ["2026-02-01", "2026-02-28"],
        ["2025-02-01", "2025-02-28"],
    ]
    explicit = from_intent(
        QueryIntent(
            entity="orders_view",
            metrics=["revenue"],
            filters=window,
            compare=IntentCompare(to="2025-02"),
        ),
        VIEW,
    )
    assert explicit.timeDimensions[0].compareDateRange == [
        ["2026-02-01", "2026-02-28"],
        ["2025-02-01", "2025-02-28"],
    ]


def test_a_derived_comparison_needs_a_bounded_window() -> None:
    with pytest.raises(ValueError, match="bounded on both ends"):
        from_intent(
            QueryIntent(
                entity="orders_view",
                metrics=["revenue"],
                compare=IntentCompare(to="previous_period"),
            ),
            VIEW,
        )


def test_a_listing_is_an_ungrouped_projection() -> None:
    q = from_intent(QueryIntent(entity="orders_view", fields=["country", "status"], limit=20), VIEW)
    assert q.ungrouped is True and q.dimensions == ["orders_view.country", "orders_view.status"]
    assert q.measures == []


def test_snapshot_rides_the_query_but_never_the_wire() -> None:
    q = from_intent(QueryIntent(entity="orders_view", metrics=["revenue"], snapshot="first"), VIEW)
    assert q.snapshot == "first" and "snapshot" not in q.wire()


def test_a_name_the_view_does_not_hold_is_refused_by_kind() -> None:
    with pytest.raises(UnknownMember) as exc:
        from_intent(QueryIntent(entity="orders_view", metrics=["margin"]), VIEW)
    assert (exc.value.kind, exc.value.name) == ("metric", "orders_view.margin")
    with pytest.raises(UnknownMember, match="definition"):
        from_intent(
            QueryIntent(entity="orders_view", metrics=["revenue"], definitions=["vip"]), VIEW
        )


def test_without_a_model_names_are_qualified_by_the_entity_and_iso_bounds_are_the_window() -> None:
    intent = QueryIntent(
        entity="orders",
        metrics=["revenue"],
        filters=[
            IntentFilter(field="ordered_at", op=">=", value="2026-01-01"),
            IntentFilter(field="country", op="=", value="FI"),
        ],
    )
    q = from_intent(intent)
    assert q.measures == ["orders.revenue"]
    assert q.timeDimensions[0].dimension == "orders.ordered_at"
    assert q.timeDimensions[0].dateRange is None, "Cube refuses a dateRange with an empty end"
    assert [f.wire() for f in q.filters] == [
        {"member": "orders.country", "operator": "equals", "values": ["FI"]},
        {"member": "orders.ordered_at", "operator": "afterOrOnDate", "values": ["2026-01-01"]},
    ]


def test_a_window_open_at_the_start_is_a_before_or_on_filter() -> None:
    intent = QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        filters=[IntentFilter(field="ordered_at", op="<", value="2026-04-01")],
    )
    q = from_intent(intent, VIEW)
    assert [f.wire() for f in q.filters] == [
        {"member": "orders_view.ordered_at", "operator": "beforeOrOnDate", "values": ["2026-03-31"]}
    ]
    assert to_intent(q).filters == [IntentFilter(field="ordered_at", op="<=", value="2026-03-31")]


def test_a_compare_needs_its_own_window_closed() -> None:
    half = [IntentFilter(field="ordered_at", op=">=", value="2026-02-01")]
    intent = QueryIntent(
        entity="orders_view", metrics=["revenue"], filters=half, compare=IntentCompare(to="2025-02")
    )
    with pytest.raises(ValueError, match="both ends"):
        from_intent(intent, VIEW)


def test_a_grain_with_no_clock_is_refused() -> None:
    with pytest.raises(UnknownMember, match="time dimension"):
        from_intent(QueryIntent(entity="orders", metrics=["revenue"], grain="month"))


def test_an_intent_without_an_entity_is_refused() -> None:
    with pytest.raises(ValueError, match="entity"):
        from_intent(QueryIntent(metrics=["revenue"]))


ROUND_TRIPS = [
    QueryIntent(entity="orders_view", metrics=["revenue"]),
    QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        grain="month",
        filters=[IntentFilter(field="ordered_at", op=">=", value="2026-01-01")],
    ),
    QueryIntent(
        entity="orders_view", metrics=["revenue", "count"], dimensions=["country"], grain="month"
    ),
    QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        definitions=["paid"],
        filters=[
            IntentFilter(field="country", op="in", value=["FI", "SE"]),
            IntentFilter(field="ordered_at", op=">=", value="2026-01-01"),
            IntentFilter(field="ordered_at", op="<=", value="2026-03-31"),
        ],
        order_by=[IntentOrder(field="revenue", dir="desc")],
        limit=10,
    ),
    QueryIntent(entity="orders_view", fields=["country", "status"], limit=50),
    QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        snapshot="last",
        filters=[IntentFilter(field="status", op="!=", value="void")],
    ),
    QueryIntent(
        entity="orders_view",
        metrics=["revenue"],
        grain="week",
        filters=[
            IntentFilter(field="ordered_at", op=">=", value="2026-02-01"),
            IntentFilter(field="ordered_at", op="<=", value="2026-02-28"),
        ],
        compare=IntentCompare(to="2025-02-01/2025-02-28"),
    ),
]


@pytest.mark.parametrize("intent", ROUND_TRIPS, ids=itertools.count())
def test_an_intent_survives_the_round_trip(intent: QueryIntent) -> None:
    back = to_intent(from_intent(intent, VIEW))
    assert back == intent
    assert from_intent(back, VIEW).sha256() == from_intent(intent, VIEW).sha256()


def test_to_intent_reads_a_cube_query_written_by_hand() -> None:
    q = _q(
        measures=["orders_view.revenue"],
        dimensions=["orders_view.country"],
        timeDimensions=[
            {
                "dimension": "orders_view.ordered_at",
                "granularity": "quarter",
                "dateRange": ["2026-01-01", "2026-06-30"],
            }
        ],
        filters=[
            {"and": [{"member": "orders_view.status", "operator": "equals", "values": ["paid"]}]}
        ],
        order={"orders_view.revenue": "desc"},
        limit=3,
    )
    intent = to_intent(q)
    assert intent.entity == "orders_view" and intent.grain == "quarter"
    assert intent.filters == [
        IntentFilter(field="status", op="=", value="paid"),
        IntentFilter(field="ordered_at", op=">=", value="2026-01-01"),
        IntentFilter(field="ordered_at", op="<=", value="2026-06-30"),
    ]
    assert intent.order_by == [IntentOrder(field="revenue", dir="desc")] and intent.limit == 3


def test_a_candidate_carries_its_ledger() -> None:
    c = Candidate(
        query=_q(measures=["orders_view.revenue"]),
        ledger=[
            Binding(word="revenue", kind="member", target="orders_view.revenue", source="declared")
        ],
        lane="typed",
    )
    assert c.model_dump()["ledger"][0]["kind"] == "member" and c.spend == {}


@pytest.mark.parametrize(
    "order",
    [
        {"v.a": "desc", "v.b": "asc"},
        [["v.a", "desc"], ["v.b", "asc"]],
        [{"v.a": "desc"}, {"v.b": "asc"}],
        [{"id": "v.a", "desc": True}, {"id": "v.b", "desc": False}],
        [{"member": "v.a", "direction": "desc"}, {"member": "v.b", "direction": "asc"}],
    ],
)
def test_every_order_spelling_reads_as_one_list_of_pairs(order: object) -> None:
    # Cube reads the object and the array of pairs alike; a lane writing
    # either (or one object per key) asks the same thing.
    q = CubeQuery.model_validate({"measures": ["v.a"], "dimensions": ["v.b"], "order": order})
    assert q.order == [("v.a", "desc"), ("v.b", "asc")]
    assert q.wire()["order"] == [["v.a", "desc"], ["v.b", "asc"]]


@pytest.mark.parametrize(
    "order", [[{"v.a": "desc", "v.b": "asc"}], [{"v.a": "sideways"}], [["v.a"]], "v.a"]
)
def test_an_order_that_names_no_single_direction_fails_validation(order: object) -> None:
    with pytest.raises(ValidationError):
        CubeQuery.model_validate({"measures": ["v.a"], "order": order})
