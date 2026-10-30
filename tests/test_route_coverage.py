"""Routing by whole-question coverage, with fallback.

A lending-library network: loans by branch and format, loans by member tier,
late fees, and a lens exposing raw tables. The router ranks lenses by
similarity with a named metric as one signal, checks that a lens holds every
word the question asks with (``router.coverage``) on the top few, and routes
to the first that does. A metric word is no longer a verdict, a tie is read
among the top lenses only (a lens named after the asked thing wins), the
decider is shown each lens's dimensions and stored values, and the routed
door hands a question a lens refuses for want of data to the next lens that
covers it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import route
from services.api.route import _LexicalEmbedder
from services.app import app
from services.config import settings
from services.contracts.lens_config import LensConfig
from services.contracts.protocols import Decision, DecisionUsage, Option
from services.contracts.response import QueryResponse
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.llm import decision_memo
from services.router import CoverageProfile, RouteDecision
from services.router.coverage import covers, held_by
from services.router.decider import LlmDecider, _options, route_question
from services.router.profiles import coverage_profile
from services.runtime.pipeline import LENS_DATA_REFUSAL

client = TestClient(app)


def _entity(
    name: str,
    metrics: list[Metric],
    dims: list[str],
    *,
    exposed: bool = False,
    grain: str | None = None,
) -> Entity:
    return Entity(
        name=name,
        source=EntitySource(connection="wh", table=f"lib.{name}"),
        grain=grain,
        fields=[Field(name="loan_id", type="string"), Field(name="loaned_on", type="date")]
        + [Field(name=d, type="string") for d in dims],
        dimensions=[Dimension(name=d) for d in dims],
        metrics=metrics,
        default_time_field="loaned_on",
        exposed=exposed,
    )


def _lens(
    name: str,
    entity: Entity,
    domains: dict[str, list[str]] | None = None,
    **cfg: object,
) -> LensBundle:
    model = SemanticModel(lens=name, dialect="duckdb", entities=[entity])
    return LensBundle(config=LensConfig(name=name, connections=["wh"], **cfg), semantic_model=model)


LOANS = _lens(
    "loans",
    _entity(
        "circulation",
        [Metric(name="loan_count", agg="count", label="Loans")],
        ["branch", "format"],
    ),
)
MEMBER_LOANS = _lens(
    "loans_by_member",
    _entity(
        "member_loans",
        [Metric(name="loan_count", agg="count", label="Loans")],
        ["member_tier"],
        grain="one row per loan to a member",
    ),
)
FEES = _lens(
    "fees",
    _entity(
        "fees",
        [Metric(name="late_fees", agg="sum", expr="fees.amount", label="Late fees")],
        ["branch"],
    ),
)
FEE_LINES = _lens(
    "fee_lines",
    _entity(
        "fee_lines",
        [
            Metric(
                name="fee_amount",
                agg="sum",
                expr="fee_lines.amount",
                description="late fees charged per loan",
            )
        ],
        ["branch", "format"],
    ),
)
DOMAINS = {
    "loans": {
        "circulation": {"format": ["ebook", "print", "audio"], "branch": ["Harbour", "Mill"]}
    },
    "loans_by_member": {"member_loans": {"member_tier": ["student", "senior", "adult"]}},
    "fees": {"fees": {"branch": ["Harbour", "Mill"]}},
    "fee_lines": {"fee_lines": {"format": ["ebook", "print"], "branch": ["Harbour", "Mill"]}},
}


def _profile(bundle: LensBundle) -> CoverageProfile:
    return coverage_profile(bundle, certified=[], domains=DOMAINS[bundle.config.name])


CATALOGUE = [_profile(b) for b in (LOANS, MEMBER_LOANS, FEES, FEE_LINES)]
BY_NAME = {p.lens: p for p in CATALOGUE}


class _Pick:
    """Picks the named lens when it is offered, else the first option."""

    typed_decisions = False

    def __init__(self, lens: str | None = None) -> None:
        self.lens = lens
        self.calls = 0
        self.shown: dict[str, str] = {}

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.calls += 1
        self.shown = {o.name: o.description for o in options}
        names = [o.name for o in options]
        chosen = self.lens if self.lens in names else names[0]
        return Decision(chosen=chosen, probs={chosen: 1.0}, provider="scripted")


def _route(
    question: str, decider: _Pick | None = None, profiles: list[CoverageProfile] = CATALOGUE
) -> RouteDecision:
    return route_question(
        profiles,
        _LexicalEmbedder(),
        LlmDecider(None, "", decider=decider or _Pick()),
        question,
    )


# ── the coverage predicate ───────────────────────────────────────────────────


def test_a_lens_covers_a_question_when_it_holds_every_word() -> None:
    held = held_by(CATALOGUE)
    assert covers(BY_NAME["loans"], "How many ebook loans at Harbour in May 2026?", held) == (
        True,
        [],
    )
    # A stored value only a sibling holds is a word this lens lacks.
    assert covers(BY_NAME["loans"], "How many loans by student members?", held) == (
        False,
        ["student", "members"],
    )
    # A word no lens holds (a verb, a synonym) separates no lens from another.
    assert covers(BY_NAME["loans"], "How many loans did patrons borrow?", held)[0]


def test_hidden_columns_hold_nothing() -> None:
    entity = LOANS.semantic_model.entities[0].model_copy(update={"hide": ["format"]})
    bundle = _lens("loans", entity)
    prof = coverage_profile(bundle, certified=[], domains=DOMAINS["loans"])
    assert not covers(prof, "ebook loans", held_by(CATALOGUE))[0]
    assert "format" not in prof.values


# ── 88: a metric word is a signal, not a verdict ─────────────────────────────


def test_a_metric_named_by_one_lens_does_not_route_to_it_without_the_filter_field() -> None:
    q = "Late fees by format at Mill last month"
    # Before: "late fees" is declared only by `fees`, which routed it there
    # on that one phrase before the decider was asked, though `fees` cannot
    # split by format.
    d = _route(q, _Pick("fee_lines"))
    assert d.lens == "fee_lines", d.reason
    assert d.chain[0] == {
        "lens": "fees",
        "result": "passed over",
        "why": "names a metric only it declares, but nothing for 'format'",
    }
    assert d.chain[-1]["lens"] == "fee_lines" and d.chain[-1]["result"] == "routed"


def test_a_metric_named_by_one_covering_lens_still_routes_without_a_model() -> None:
    decider = _Pick()
    d = _route("Late fees at Mill last month", decider)
    assert (d.lens, d.decided_by, decider.calls) == ("fees", "declared-name", 0)


def test_the_decider_pick_is_passed_over_when_it_lacks_a_word() -> None:
    d = _route("How many loans by student members?", _Pick("loans"))
    assert d.lens == "loans_by_member"
    assert d.decided_by == "coverage"
    assert {
        "lens": "loans",
        "result": "passed over",
        "why": "nothing for 'student', 'members'",
    } in d.chain


def test_none_covering_names_the_closest_lens_and_the_words_it_lacks() -> None:
    d = _route("Late fees by member tier", _Pick("fees"))
    assert not d.covered
    assert d.reason == (
        "no lens covers every word of this question: the closest, fees, "
        "has nothing for 'member', 'tier'"
    )


class _Split:
    """Cannot tell the two lenses apart: half its vote to each."""

    typed_decisions = False

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        return Decision(
            chosen="loans", probs={"loans": 0.5, "loans_by_member": 0.5}, provider="scripted"
        )


def test_coverage_settles_a_pair_the_decider_could_not_separate() -> None:
    d = route_question(
        CATALOGUE,
        _LexicalEmbedder(),
        LlmDecider(None, "", decider=_Split()),
        "Loans by member tier for students",
    )
    assert (d.covered, d.lens, d.decided_by) == (True, "loans_by_member", "coverage"), d.reason
    assert "which the decider could not separate" in d.reason


# ── 78: the tie is read in the shortlist, and reads lens names ───────────────


def test_a_lens_named_after_the_asked_thing_wins_the_tie() -> None:
    # Before: two lenses declare the "loans" metric, so the question was
    # asked which lens, though one of them is called loans.
    d = _route("How many loans in May 2026?")
    assert (d.covered, d.lens) == (True, "loans"), d.reason


def test_the_tie_counts_only_lenses_that_cover_the_question() -> None:
    d = _route("How many loans by member tier in May 2026?")
    assert (d.covered, d.lens) == (True, "loans_by_member"), d.reason


def test_a_tie_no_name_settles_is_still_asked() -> None:
    a = _profile(MEMBER_LOANS)
    twin = coverage_profile(
        _lens("lending_desk", MEMBER_LOANS.semantic_model.entities[0]),
        certified=[],
        domains=DOMAINS["loans_by_member"],
    )
    d = _route("Loans for senior members", profiles=[a, twin])
    assert not d.covered and d.verdict == "clarify"
    assert "several lenses measure loans: lending_desk, loans_by_member" in d.reason


# ── the decider sees dimensions and stored values ────────────────────────────


def test_the_decider_sees_each_lens_dimensions_and_values() -> None:
    shown = {o.name: o.description for o in _options(CATALOGUE)}
    assert "Split or filtered by: branch, format." in shown["loans"]
    assert "Stored values: branch: Harbour, Mill; format: ebook, print, audio." in shown["loans"]
    assert "member_tier: student, senior, adult" in shown["loans_by_member"]


def test_stored_values_shown_are_bounded() -> None:
    many = {"circulation": {"format": [f"v{i}" for i in range(40)]}}
    prof = coverage_profile(LOANS, certified=[], domains=many)
    assert len(prof.values["format"]) == 8


# ── the door: a refusal for missing data falls through ───────────────────────


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


def _org_token() -> tuple[object, str]:
    from services.auth.tokens import hash_token, new_admin_token

    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('CovT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _routed(fallbacks: list[str]) -> RouteDecision:
    return RouteDecision(
        True,
        "loans",
        0.8,
        [("loans_by_member", 0.7)],
        "routed (llm)",
        decided_by="decider",
        chain=[{"lens": "loans", "result": "routed", "why": "covers every word of the question"}],
        fallbacks=fallbacks,
    )


@needs_db
@pytest.mark.parametrize(
    ("first", "served"),
    [
        (f"{LENS_DATA_REFUSAL}this lens has no member tier", "loans_by_member"),
        # A governed refusal is the answer: it never falls through.
        ("Loan value is not computable here; ask the finance lens.", "loans"),
    ],
)
def test_a_refusal_for_missing_data_falls_through_to_the_next_lens(
    monkeypatch: pytest.MonkeyPatch, first: str, served: str
) -> None:
    org, raw = _org_token()
    asked: list[str] = []

    def fake_run(name: str, q: str, *a: object, **k: object) -> QueryResponse:
        asked.append(name)
        if name == "loans":
            return QueryResponse(lens=name, status="refused", answer=first, request_id="req_a")
        return QueryResponse(lens=name, answer="31 loans", request_id="req_b")

    monkeypatch.setattr(route, "_route", lambda caller, q: _routed(["loans_by_member"]))
    monkeypatch.setattr(route, "run_lens_query", fake_run)
    monkeypatch.setattr(route, "_not_for_named", lambda *a: None)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "loans by member tier"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["routed_to"]["lens"] == served
        assert asked == (["loans", "loans_by_member"] if served != "loans" else ["loans"])
        with create_engine(settings.database_admin_url).connect() as c:
            row = c.execute(
                text("SELECT routed_lens, decision FROM routing_decision WHERE org_id = :o"),
                {"o": org},
            ).one()
        assert row[0] == served
        chain = row[1]["chain"]
        if served == "loans_by_member":
            assert body["routed_to"]["decided_by"] == "fallback"
            assert [(c["lens"], c["result"]) for c in chain] == [
                ("loans", "routed"),
                ("loans", "refused"),
                ("loans_by_member", "answered"),
            ]
        else:
            assert [(c["lens"], c["result"]) for c in chain] == [("loans", "routed")]
    finally:
        _cleanup(org)


@needs_db
def test_no_covering_lens_goes_to_the_explore_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    uncovered = RouteDecision(
        False,
        None,
        0.4,
        [],
        "no lens covers every word of this question: the closest, loans, "
        "has nothing for 'reshelving'",
        decided_by="coverage",
        chain=[{"lens": "loans", "result": "passed over", "why": "nothing for 'reshelving'"}],
    )
    served: list[tuple[str, object]] = []

    def fake_run(
        name: str,
        q: str,
        caller: object,
        bg: object,
        fmt: str,
        b: object,
        untyped: object,
        *a: object,
        **k: object,
    ) -> QueryResponse:
        served.append((name, untyped))
        return QueryResponse(lens=name, answer="12 reshelving events", request_id="req_x")

    monkeypatch.setattr(route, "_route", lambda caller, q: uncovered)
    monkeypatch.setattr(route, "run_lens_query", fake_run)
    monkeypatch.setattr(route, "lens_refusal_named", lambda *a: None)
    monkeypatch.setattr(route, "_not_for_named", lambda *a: None)
    monkeypatch.setattr(route, "_explore_lens", lambda q, caller, untyped: "stacks")
    try:
        r = client.post(
            "/v1/query", json={"q": "reshelving events"}, headers={"Authorization": f"Bearer {raw}"}
        )
        body = r.json()
        assert body["routed_to"] == {"lens": "stacks", "score": 0.0, "decided_by": "explore"}
        assert served == [("stacks", None)]
        with create_engine(settings.database_admin_url).connect() as c:
            row = c.execute(
                text(
                    "SELECT routed_lens, covered, decision FROM routing_decision WHERE org_id = :o"
                ),
                {"o": org},
            ).one()
        # Served from exposed tables, still counted as a gap in governed coverage.
        assert (row[0], row[1]) == ("stacks", False)
        assert row[2]["chain"][-1]["result"] == "explore"
    finally:
        _cleanup(org)


def test_the_explore_scope_is_a_lens_exposing_tables_that_answers_untyped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stacks = _entity("reshelving_events", [], ["shelf"], exposed=True)
    bundles = [
        ("loans", "", "", LOANS),
        ("stacks", "", "", _lens("stacks", stacks, untyped_fallback=True)),
        ("stacks_typed", "", "", _lens("stacks_typed", stacks, untyped_fallback=False)),
    ]
    monkeypatch.setattr(route, "list_published_for_org", lambda org: bundles)
    monkeypatch.setattr(route, "authorize", lambda caller, cfg: (True, ""))
    caller = type("C", (), {"org_id": "o"})()
    assert route._explore_lens("reshelving events by shelf", caller, None) == "stacks"  # type: ignore[arg-type]
    # A caller demanding typed answers has no explore scope.
    assert route._explore_lens("reshelving events by shelf", caller, False) is None  # type: ignore[arg-type]
    # Nothing exposed holds a word of it: no explore scope either.
    assert route._explore_lens("weather tomorrow", caller, None) is None  # type: ignore[arg-type]


# ── decisions shared across the lenses typed for one question ────────────────


class _Billed:
    typed_decisions = True

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        self.calls += 1
        return Decision(
            chosen=options[0].name,
            probs=None,
            provider="scripted",
            usage=DecisionUsage(calls=1, input_tokens=100, output_tokens=5),
        )


def test_a_repeated_decision_is_asked_once_and_billed_once() -> None:
    inner = _Billed()
    opts = [Option("loan_count", "loans"), Option("late_fees", "fees")]
    assert decision_memo.memoised(inner) is inner  # no memo open: as it is
    with decision_memo.shared():
        d = decision_memo.memoised(inner)
        assert d is not None
        first = d.decide(question="q", context="metric", options=opts, allow_none=False)
        again = decision_memo.memoised(inner).decide(  # type: ignore[union-attr]
            question="q", context="metric", options=opts, allow_none=False
        )
        other = d.decide(question="q", context="metric", options=opts[:1], allow_none=False)
        assert d.typed_decisions is True  # type: ignore[attr-defined]
    assert inner.calls == 2
    assert first.usage is not None and first.usage.input_tokens == 100
    assert again.chosen == first.chosen
    assert again.usage is not None and (again.usage.calls, again.usage.input_tokens) == (0, 0)
    assert other.usage is not None and other.usage.calls == 1


class _Batched(_Billed):
    def decide_many(
        self, question: str, specs: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Decision]:
        return {
            k: self.decide(question=question, context=c, options=o, allow_none=n)
            for k, (c, o, n) in specs.items()
        }


def test_a_batched_decision_already_made_is_not_asked_again() -> None:
    inner = _Batched()
    opts = [Option("loan_count", "loans")]
    with decision_memo.shared():
        d = decision_memo.memoised(inner)
        first = d.decide_many("q", {"metric": ("metric", opts, False)})  # type: ignore[union-attr]
        both = d.decide_many(  # type: ignore[union-attr]
            "q", {"metric": ("metric", opts, False), "branch": ("branch", opts, True)}
        )
    assert inner.calls == 2
    assert first["metric"].usage is not None and first["metric"].usage.calls == 1
    assert both["metric"].usage is not None and both["metric"].usage.calls == 0
    assert both["branch"].usage is not None and both["branch"].usage.calls == 1
