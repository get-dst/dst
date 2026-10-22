"""Sibling lenses: routing that separates marts on one subject deterministically.

A fixture catalogue of sibling lenses whose ``use_when`` asks overlap verbatim,
plus single-question lenses, routed with no embedding provider (the lexical
matcher) and a scripted decider that cannot tell siblings apart: it splits its
vote between the first two lenses it is shown. The headline is the share of
certified questions that reach the lens that certifies them, before (scoring
every anchor, no certified shortcut) and after (``route_question``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.route import _LexicalEmbedder
from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import Entity, EntitySource, SemanticModel
from services.router import CoverageProfile, Router
from services.router.decider import LlmDecider, route_question, route_with_llm
from services.router.profiles import _not_for
from services.router.sibling import (
    certified_owner,
    distinct_profiles,
    drop_not_for,
    normalise,
    unique_name_owner,
)
from services.runtime.not_for import not_for_refusal

SHARED_SALES = ["how are ticket sales trending", "ticket sales by month"]
SHARED_REFUNDS = ["refunds and exchanges", "net refund rate"]
SHARED_PASS = ["season pass usage"]


def _p(lens: str, anchors: list[str], certified: list[str], names: list[str]) -> CoverageProfile:
    # Certified-answer questions are not coverage anchors (the profile is built
    # from the lens's files; the certified library lives in the database), which
    # is why a lens's own certified question could miss it.
    return CoverageProfile(lens, anchors, certified=certified, names=names)


CATALOGUE = [
    _p(
        "sales_daily",
        [*SHARED_SALES, "daily ticket sales"],
        ["What were ticket sales yesterday?", "Ticket sales per day for the last week"],
        ["daily_ticket_sales", "mart.fct_sales_daily"],
    ),
    _p(
        "sales_trend",
        [*SHARED_SALES, "ticket sales growth"],
        ["How did ticket sales grow quarter over quarter?", "Ticket sales trend for this year"],
        ["ticket_sales_growth", "mart.fct_sales_trend"],
    ),
    _p(
        "seat_refunds",
        [*SHARED_REFUNDS, "seat refunds"],
        ["How many seats did we refund last month?"],
        ["seats_refunded", "mart.seat_refunds"],
    ),
    _p(
        "booking_refunds",
        [*SHARED_REFUNDS, "booking refunds"],
        ["What share of bookings was refunded in March?"],
        ["bookings_refunded", "mart.bookings"],
    ),
    _p(
        "box_office_kpis",
        [*SHARED_REFUNDS, *SHARED_SALES, "average ticket yield"],
        ["What is the net refund rate this quarter?", "Average ticket yield last quarter"],
        ["refund_rate", "ticket_yield", "mart.box_office_kpis"],
    ),
    _p(
        "season_pass_holders",
        [*SHARED_PASS, "season pass holders"],
        ["How many patrons bought a season pass this month?"],
        ["passes_held", "mart.season_pass_holders"],
    ),
    _p(
        "pass_scans",
        [*SHARED_PASS, "pass scans"],
        ["How many season passes were scanned at the doors yesterday?"],
        ["pass_scan_count", "mart.pass_scans"],
    ),
    # Single-question lenses: one certified question, nothing else to score.
    _p("producer_report", [], ["Numbers for the weekly producer report"], ["producer_report"]),
    _p("door_queue", [], ["Share of patrons admitted within ten minutes"], ["admitted_on_time"]),
]


class _SplitVote:
    """Cannot tell siblings apart: half its vote each to the first two options."""

    typed_decisions = False

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        a, b = options[0].name, options[1].name if len(options) > 1 else "none"
        return Decision(chosen=a, probs={a: 0.5, b: 0.5}, provider="scripted")


def _decider() -> LlmDecider:
    return LlmDecider(None, "", decider=_SplitVote())


def _before(question: str) -> str | None:
    """Every anchor scored, no certified shortcut, no name tie-break input."""
    raw = [CoverageProfile(p.lens, p.anchors) for p in CATALOGUE]
    router = Router(raw, _LexicalEmbedder())
    return route_with_llm(router, {p.lens: p for p in raw}, _decider(), question).lens


def _after(question: str, profiles: list[CoverageProfile] = CATALOGUE) -> str | None:
    return route_question(profiles, _LexicalEmbedder(), _decider(), question).lens


def _share(route: object, cases: list[tuple[str, str]]) -> float:
    return sum(route(q) == lens for q, lens in cases) / len(cases)  # type: ignore[operator]


CERTIFIED = [(q, p.lens) for p in CATALOGUE for q in p.certified]


def test_certified_questions_reach_their_lens_before_and_after() -> None:
    assert len({p.lens for p in CATALOGUE}) >= 6
    before, after = _share(_before, CERTIFIED), _share(_after, CERTIFIED)
    print(f"\ncertified routed to owner: before {before:.0%}, after {after:.0%}")
    assert before < 0.5  # the fixture reproduces the sibling failure
    assert after == 1.0


def test_a_restyled_certified_question_still_matches() -> None:
    d = route_question(
        CATALOGUE, _LexicalEmbedder(), _decider(), "  numbers FOR the weekly   producer report "
    )
    assert (d.lens, d.decided_by) == ("producer_report", "certified")


def test_a_question_two_lenses_certify_is_left_to_the_decider() -> None:
    twin = [_p("a", [], ["Total tickets sold"], []), _p("b", [], ["total tickets sold?"], [])]
    assert certified_owner("total tickets sold", twin) is None


def test_shared_anchors_are_not_scored_for_any_lens_that_shares_them() -> None:
    by_lens = {p.lens: p for p in distinct_profiles(CATALOGUE)}
    assert "ticket sales by month" not in by_lens["sales_daily"].anchors
    assert "ticket sales by month" not in by_lens["box_office_kpis"].anchors
    assert "daily ticket sales" in by_lens["sales_daily"].anchors


def test_a_named_table_breaks_a_two_lens_tie() -> None:
    pair = [p for p in CATALOGUE if p.lens in ("season_pass_holders", "pass_scans")]
    assert unique_name_owner("season pass usage from pass_scans", pair) == "pass_scans"
    assert unique_name_owner("pass scan count by venue", pair) == "pass_scans"
    # A name both declare, or none, decides nothing.
    assert unique_name_owner("season pass usage", pair) is None


def test_the_decider_tie_between_two_siblings_goes_to_the_named_one() -> None:
    q = "season pass usage in mart.pass_scans"
    assert _before(q) is None  # split vote → clarify → no route
    d = route_question(CATALOGUE, _LexicalEmbedder(), _decider(), q)
    assert (d.lens, d.decided_by) == ("pass_scans", "declared-name")


def test_not_for_removes_the_lens_from_the_candidates() -> None:
    guarded = [
        CoverageProfile(p.lens, p.anchors, certified=p.certified, names=p.names, not_for=nf)
        for p in CATALOGUE
        for nf in [["booking-level refunds"] if p.lens == "seat_refunds" else []]
    ]
    q = "How many seats did we refund last month?"
    assert _after(q, guarded) == "seat_refunds"
    kept = drop_not_for("booking-level refunds last month", guarded)
    assert "seat_refunds" not in {p.lens for p in kept}
    assert len(kept) == len(guarded) - 1


def test_not_for_is_read_from_mappings_models_or_its_absence() -> None:
    empty = SemanticModel(lens="l", dialect="duckdb")
    assert _not_for(SimpleNamespace(), empty) == []
    cfg = SimpleNamespace(
        not_for=[{"ask": "door-scan attendance", "sibling": "s"}, SimpleNamespace(ask="show date")]
    )
    assert _not_for(cfg, empty) == ["door-scan attendance", "show date"]


def test_the_profile_carries_the_asks_the_lens_door_refuses() -> None:
    """The router drops a lens on the same asks its own door refuses: the
    lens's ``not_for`` and its single entity's ``Avoid for:`` use cases."""
    sm = SemanticModel(
        lens="seat_refunds",
        dialect="duckdb",
        entities=[
            Entity(
                name="seat_refunds",
                source=EntitySource(connection="wh", table="seat_refunds"),
                use_cases=["Avoid for: booking-level refunds"],
            )
        ],
        not_for=[{"ask": "door-scan attendance", "sibling": "attendance"}],
    )
    assert _not_for(SimpleNamespace(), sm) == ["door-scan attendance", "booking-level refunds"]


@pytest.mark.parametrize(
    ("question", "refused"),
    [
        ("booking-level refunds last month", True),
        ("refunds at the booking level last month", True),
        ("How many seats did we refund last month?", False),
        ("bookings lost to refunds", False),
    ],
)
def test_the_router_and_the_lens_door_share_one_matcher(question: str, refused: bool) -> None:
    """A question the lens's door refuses is one the router drops it for, and
    a question it answers keeps it a candidate: one matcher, no gap between."""
    ask = "booking-level refunds"
    profile = CoverageProfile("seat_refunds", ["seat refunds"], not_for=[ask])
    sm = SemanticModel(lens="seat_refunds", dialect="duckdb", not_for=[{"ask": ask}])
    assert (not_for_refusal(question, sm) is not None) is refused
    assert (drop_not_for(question, [profile]) == []) is refused


def test_normalise_folds_case_space_and_trailing_punctuation() -> None:
    assert normalise("  What  were Ticket Sales?? ") == "what were ticket sales"
