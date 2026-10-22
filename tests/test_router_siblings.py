"""Sibling lenses: routing that separates marts on one subject deterministically.

A fixture catalogue of sibling lenses whose ``use_when`` asks overlap verbatim,
plus single-question lenses, routed with no embedding provider (the lexical
matcher) and a scripted decider that cannot tell siblings apart: it splits its
vote between the first two lenses it is shown. The headline is the share of
certified questions that reach the lens that certifies them, before (scoring
every anchor, no certified shortcut) and after (``route_question``).
"""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.route import _LexicalEmbedder
from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.router import CoverageProfile, RouteDecision, Router
from services.router import _cosine as _cos
from services.router.decider import LlmDecider, route_question, route_with_llm
from services.router.profiles import _not_for, coverage_profile
from services.router.sibling import (
    certified_owner,
    distinct_profiles,
    drop_not_for,
    metric_name_owner,
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


# ---------------------------------------------------------------------------
# not_for on every router path. The literal match drops a lens when the question
# names every content word of an ask; a paraphrase names none of them, and the
# similarity that ranks lenses must not then carry the ask to the lens that
# declared it is not for it.

_CONCEPTS = {
    "refund": {"refund", "refunds", "refunded"},
    "booking": {"booking", "bookings", "order", "orders"},
    "seat": {"seat", "seats"},
    "total": {"total", "totals"},
    "season": {"season"},
    "cancel": {"cancelled", "cancellations"},
}


class _ConceptEmbedder:
    """A fake semantic embedder: synonyms share an axis, so "refunds per order"
    sits near "booking-level refunds" while sharing only one word with it."""

    dim = len(_CONCEPTS)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            words = set(re.findall(r"[a-z]+", t.lower()))
            out.append([1.0 if words & syn else 0.0 for syn in _CONCEPTS.values()])
        return out


class _TopPick:
    """Acts on the first option it is shown (the best-ranked lens); records them."""

    typed_decisions = False

    def __init__(self) -> None:
        self.seen: list[Option] = []

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.seen = list(options)
        return Decision(chosen=options[0].name, probs={options[0].name: 1.0}, provider="scripted")


SEATS = CoverageProfile(
    "seat_refunds",
    ["total refunds this season", "seat refunds"],
    not_for=["booking-level refunds"],
)
BOOKINGS = CoverageProfile("booking_refunds", ["cancelled bookings"])
PARAPHRASE = "total refunds per order"


def test_the_paraphrase_is_not_the_literal_ask() -> None:
    assert drop_not_for(PARAPHRASE, [SEATS, BOOKINGS]) == [SEATS, BOOKINGS]


def test_a_paraphrase_of_a_not_for_ask_never_ranks_the_lens() -> None:
    scored = Router([SEATS, BOOKINGS], _ConceptEmbedder()).scored(PARAPHRASE)
    assert [lens for lens, _ in scored] == ["booking_refunds"]
    # What the lens does cover still ranks it first.
    covered = Router([SEATS, BOOKINGS], _ConceptEmbedder()).scored("seat refunds this season")
    assert covered[0][0] == "seat_refunds"


def test_the_decider_never_sees_the_lens_for_a_paraphrased_ask() -> None:
    pick = _TopPick()
    d = route_question(
        [SEATS, BOOKINGS], _ConceptEmbedder(), LlmDecider(None, "", decider=pick), PARAPHRASE
    )
    assert d.lens == "booking_refunds"
    assert [o.name for o in pick.seen] == ["booking_refunds"]


def test_the_stored_scorer_path_drops_it_too() -> None:
    """Anchors scored where they are stored (pgvector) still meet the not_for asks."""
    embedder = _ConceptEmbedder()
    memory = Router([SEATS, BOOKINGS], embedder)

    def stored(qv: list[float]) -> list[tuple[str, float]]:
        return sorted(
            (
                (p.lens, max(_cos(qv, v) for v in embedder.embed(p.anchors)))
                for p in (SEATS, BOOKINGS)
            ),
            key=lambda x: -x[1],
        )

    scored = Router([SEATS, BOOKINGS], embedder, scorer=stored).scored(PARAPHRASE)
    assert scored == memory.scored(PARAPHRASE) == [("booking_refunds", scored[0][1])]


def test_the_decider_reads_what_each_lens_is_not_for() -> None:
    pick = _TopPick()
    LlmDecider(None, "", decider=pick).decide("refunds", [SEATS, BOOKINGS])
    by_name = {o.name: o.description for o in pick.seen}
    assert "Not for: booking-level refunds." in by_name["seat_refunds"]
    assert "Not for" not in by_name["booking_refunds"]


def test_avoid_for_lines_are_never_scoring_anchors() -> None:
    """On a lens with several entities an `Avoid for:` line is no refusal (the
    other entity may answer it), and it is no anchor either: as one it pulled
    the very ask it names toward the lens."""
    sm = SemanticModel(
        lens="box_office",
        dialect="duckdb",
        entities=[
            Entity(
                name="seat_refunds",
                source=EntitySource(connection="wh", table="seat_refunds"),
                use_cases=["Use when: seat refunds by show", "Avoid for: booking-level refunds"],
            ),
            Entity(name="shows", source=EntitySource(connection="wh", table="shows")),
        ],
    )
    cfg = SimpleNamespace(
        name="box_office",
        display_name="Box office",
        description="",
        model=SimpleNamespace(certified_dir=None),
    )
    prof = coverage_profile(SimpleNamespace(config=cfg, semantic_model=sm), certified=[])  # type: ignore[arg-type]
    assert "Use when: seat refunds by show" in prof.anchors
    assert not any("booking-level" in a for a in prof.anchors)


# ---------------------------------------------------------------------------
# A metric named by its name, label or alias routes to the one lens declaring
# it, ahead of similarity: a sibling whose sample questions read closer must
# not take the figure the other lens computes.

YIELD = CoverageProfile(
    "ticket_yield",
    ["avg ticket yield"],
    names=["avg_ticket_yield"],
    metric_names=[
        "avg_ticket_yield",
        "avg ticket yield",
        "Average ticket yield",
        "yield per ticket",
    ],
)
TREND = CoverageProfile(
    "sales_trend",
    ["ticket yield per quarter last year", "ticket sales by quarter"],
    names=["ticket_sales"],
    metric_names=["ticket_sales", "ticket sales"],
)
KPIS = CoverageProfile(
    "box_office_kpis",
    ["box office kpis per quarter"],
    metric_names=["net_takings", "net takings", "takings"],
)
NAMED = "yield per ticket last quarter"


def _top(question: str, profiles: list[CoverageProfile]) -> RouteDecision:
    return route_question(
        profiles, _LexicalEmbedder(), LlmDecider(None, "", decider=_TopPick()), question
    )


def test_similarity_alone_hands_the_named_metric_to_the_sibling() -> None:
    raw = [YIELD, TREND, KPIS]
    router = Router(raw, _LexicalEmbedder())
    assert router.scored(NAMED)[0][0] == "sales_trend"
    picked = route_with_llm(
        router, {p.lens: p for p in raw}, LlmDecider(None, "", decider=_TopPick()), NAMED
    )
    assert picked.lens == "sales_trend"


@pytest.mark.parametrize(
    "question",
    [
        "Average ticket yield by month",
        "avg_ticket_yield in Q3",
        "What is the AVG ticket yield?",
    ],
)
def test_a_metric_label_or_alias_routes_to_its_lens(question: str) -> None:
    d = _top(question, [YIELD, TREND, KPIS])
    assert (d.lens, d.decided_by) == ("ticket_yield", "declared-name")


def test_a_metric_two_lenses_declare_decides_nothing() -> None:
    twin = replace(KPIS, lens="kpis_copy")
    assert metric_name_owner("net takings last quarter", [KPIS, twin]) is None
    # ...but a metric only one of them declares still does.
    own = replace(twin, metric_names=[*twin.metric_names, "takings per seat"])
    assert metric_name_owner("takings per seat and net takings", [KPIS, own]) == "kpis_copy"


def test_the_longer_phrase_named_wins_and_two_owners_name_none() -> None:
    gross = CoverageProfile("gross", [], metric_names=["takings"])
    net = CoverageProfile("net", [], metric_names=["net takings"])
    assert metric_name_owner("net takings this week", [gross, net]) == "net"
    assert metric_name_owner("takings this week", [gross, net]) == "gross"
    assert metric_name_owner("net takings and ticket sales", [net, TREND]) is None
    # A metric called by an aggregation word alone names no figure.
    counted = CoverageProfile("counted", [], metric_names=["count", "total"])
    assert metric_name_owner("total refunds this season", [counted, gross]) is None


def test_the_certified_question_still_comes_first() -> None:
    certifying = replace(TREND, certified=[NAMED])
    d = _top(NAMED, [YIELD, certifying, KPIS])
    assert (d.lens, d.decided_by) == ("sales_trend", "certified")


def test_a_named_metric_never_routes_to_a_lens_not_for_the_ask() -> None:
    owner = replace(SEATS, metric_names=["refunds", "total refunds"])
    d = route_question(
        [owner, BOOKINGS],
        _ConceptEmbedder(),
        LlmDecider(None, "", decider=_TopPick()),
        PARAPHRASE,
    )
    assert d.lens == "booking_refunds" and d.decided_by != "declared-name"


# ---------------------------------------------------------------------------
# Two lenses over one entity carry the same anchors (its name, its metrics), so
# neither scores on them. A lens left with no anchor of its own must still reach
# the decider, and the decider reads each lens whole, time axis included.


class _TrendReader:
    """Routes a trend ask to a lens that says it splits over time, else none."""

    typed_decisions = False

    def __init__(self) -> None:
        self.seen: list[Option] = []

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.seen = list(options)
        chosen = next((o.name for o in options if "Over time by" in o.description), None)
        return Decision(chosen=chosen, probs=None, provider="scripted")


ENTITY = ["performances", "tickets sold"]
SNAPSHOT = CoverageProfile("show_kpis", [*ENTITY, "today's show kpis snapshot"])
TRENDS = CoverageProfile("show_trends", list(ENTITY), time_fields=["performed_at"])
MONTHLY = "tickets sold by month"


def test_a_month_split_reaches_the_lens_that_shares_every_anchor() -> None:
    reader = _TrendReader()
    d = route_question(
        [SNAPSHOT, TRENDS], _LexicalEmbedder(), LlmDecider(None, "", decider=reader), MONTHLY
    )
    assert d.lens == "show_trends", d.reason
    shown = {o.name: o.description for o in reader.seen}
    assert set(shown) == {"show_kpis", "show_trends"}
    # The decider is told what each lens covers, shared anchors included.
    assert "tickets sold" in shown["show_kpis"] and "tickets sold" in shown["show_trends"]
    assert "Over time by: performed_at." in shown["show_trends"]


def test_siblings_with_no_anchor_of_their_own_are_not_a_decline() -> None:
    twin = replace(TRENDS, lens="show_trends_eu")
    reader = _TrendReader()
    d = route_question(
        [TRENDS, twin], _LexicalEmbedder(), LlmDecider(None, "", decider=reader), MONTHLY
    )
    assert d.reason != "no lenses to route to"
    assert {o.name for o in reader.seen} == {"show_trends", "show_trends_eu"}


def test_the_profile_carries_metric_phrases_and_time_fields() -> None:
    sm = SemanticModel(
        lens="show_trends",
        dialect="duckdb",
        entities=[
            Entity(
                name="performances",
                source=EntitySource(connection="wh", table="performances"),
                default_time_field="performed_at",
                fields=[
                    Field(name="performed_at", type="date"),
                    Field(name="booked_at", type="date"),
                    Field(name="seats", type="number"),
                ],
                metrics=[
                    Metric(
                        name="tickets_sold",
                        agg="sum",
                        expr="performances.seats",
                        label="Tickets sold",
                        aliases=["seats sold"],
                    ),
                    Metric(
                        name="tickets_booked",
                        agg="sum",
                        expr="performances.seats",
                        agg_time_field="booked_at",
                    ),
                ],
            )
        ],
    )
    cfg = SimpleNamespace(
        name="show_trends",
        display_name="",
        description="",
        model=SimpleNamespace(certified_dir=None),
    )
    prof = coverage_profile(SimpleNamespace(config=cfg, semantic_model=sm), certified=[])  # type: ignore[arg-type]
    assert {"tickets_sold", "tickets sold", "Tickets sold"} <= set(prof.metric_names)
    # An alias widens the metric slot inside the lens, not what routes to it.
    assert "seats sold" not in prof.metric_names
    assert prof.time_fields == ["performed_at", "booked_at"]


# ---------------------------------------------------------------------------
# An alias is how a lens reads its own metric slot. As routing vocabulary it
# let a lens capture questions its sibling answered the day it was added, so it
# routes nothing: a question saying only the alias is ranked like any other.


def test_an_alias_alone_routes_nothing() -> None:
    sm = SemanticModel(
        lens="ticket_yield",
        dialect="duckdb",
        entities=[
            Entity(
                name="tickets",
                source=EntitySource(connection="wh", table="tickets"),
                fields=[Field(name="price", type="number")],
                metrics=[
                    Metric(
                        name="avg_ticket_yield",
                        agg="avg",
                        expr="tickets.price",
                        label="Average ticket yield",
                        aliases=["takings"],
                    )
                ],
            )
        ],
    )
    cfg = SimpleNamespace(
        name="ticket_yield",
        display_name="",
        description="",
        model=SimpleNamespace(certified_dir=None),
    )
    prof = coverage_profile(SimpleNamespace(config=cfg, semantic_model=sm), certified=[])  # type: ignore[arg-type]
    assert "takings" not in prof.metric_names
    # Before, both lenses declared "takings" and it named neither; now the
    # lens whose metric is called that keeps it.
    assert metric_name_owner("takings this week", [prof, KPIS]) == "box_office_kpis"


# ---------------------------------------------------------------------------
# A metric several lenses declare (one shared layer, selected by siblings) is
# theirs to split by what they declare; otherwise the question is asked which
# lens. A decider shown the siblings picks one, and nothing says it guessed.

DAILY = CoverageProfile(
    "door_daily",
    ["door scans per day"],
    names=["mart.door_daily"],
    metric_names=["door_scans", "door scans"],
    time_fields=["scanned_on"],
)
MONTHLY_KPIS = CoverageProfile(
    "door_kpis",
    ["door kpis this month"],
    names=["mart.door_kpis"],
    metric_names=["door_scans", "door scans"],
    time_fields=["month_start"],
)
NOW = CoverageProfile(
    "door_snapshot",
    ["door scans so far tonight"],
    names=["mart.door_snapshot"],
    metric_names=["door_scans", "door scans"],
)


def test_a_metric_two_lenses_declare_asks_which_lens() -> None:
    d = _top("door scans last week", [DAILY, MONTHLY_KPIS])
    assert not d.covered and d.verdict == "clarify"
    assert (
        d.reason == "several lenses measure door scans: door_daily, door_kpis. Name the lens to ask"
    )


def test_a_name_only_one_owner_declares_picks_it() -> None:
    d = _top("door scans last week from door_kpis", [DAILY, MONTHLY_KPIS])
    assert (d.lens, d.decided_by) == ("door_kpis", "declared-name")


def test_a_split_over_time_only_one_owner_makes_picks_it() -> None:
    d = _top("door scans by month", [DAILY, NOW])
    assert (d.lens, d.decided_by) == ("door_daily", "declared-name")


def test_owners_that_cannot_split_over_time_say_so() -> None:
    twin = replace(NOW, lens="door_snapshot_eu")
    d = _top("door scans by month", [NOW, twin])
    assert not d.covered
    assert d.reason == (
        "door_snapshot, door_snapshot_eu measure door scans, and none of them can split "
        "it by month: they declare no time field"
    )


def test_a_metric_one_owner_declares_alone_still_routes() -> None:
    own = replace(DAILY, metric_names=[*DAILY.metric_names, "scan rate"])
    d = _top("door scans and scan rate last week", [own, MONTHLY_KPIS])
    assert (d.lens, d.decided_by) == ("door_daily", "declared-name")
