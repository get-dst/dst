"""Routing among lenses served through Cube.

A small library network over a Cube model: loans (by branch, format), late
fees (by branch), member loans (by member tier). Each lens is a view; its
routing profile is read from the ``LensModel`` (view members, titles,
descriptions, ``meta.dst`` aliases, the probe's dictionaries, the overlay's
company names and not-computable terms), never from entity YAML. Coverage
decides first: one lens holding the question's words routes with no model
asked, and the decider is asked only when several lenses hold them equally,
with the whole catalogue in its (cached) context. At the door, a lens that
declares the ask another lens's, or whose lane finds nothing in it, hands the
question to the next lens.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import route
from services.api.route import _LexicalEmbedder, _refused_for_data
from services.app import app
from services.config import settings
from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import LensModel, parse_overlay
from services.contracts.protocols import CacheableBlock, Decision, LLMResult, Message, Option
from services.contracts.response import QueryResponse
from services.contracts.semantic_model import SemanticModel
from services.lenses.store import LensBundle
from services.llm.vote_decider import VoteDecider
from services.router import CoverageProfile, RouteDecision
from services.router.coverage import vocabulary
from services.router.decider import LlmDecider, route_question
from services.router.profiles import cube_coverage_profile
from services.runtime.pipeline import LENS_DATA_REFUSAL

client = TestClient(app)


def _measure(view: str, cube: str, name: str, title: str, **dst: Any) -> dict[str, Any]:
    return {
        "name": f"{view}.{name}",
        "aliasMember": f"{cube}.{name}",
        "title": title,
        "shortTitle": title,
        "aggType": "count",
        "type": "number",
        "meta": {"dst": dst} if dst else {},
    }


def _dim(view: str, cube: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": f"{view}.{name}",
        "aliasMember": f"{cube}.{name}",
        "title": name,
        "type": "string",
        **extra,
    }


def _view(name: str, measures: list[dict[str, Any]], dims: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "type": "view", "measures": measures, "dimensions": dims, "segments": []}


def _cube(name: str, measures: list[str], dims: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "cube",
        "measures": [
            {"name": f"{name}.{m}", "aggType": "count", "type": "number"} for m in measures
        ],
        "dimensions": [{"name": f"{name}.{d}", "type": "string"} for d in dims],
        "segments": [],
    }


META = MetaDoc.parse(
    {
        "cubes": [
            _cube("circulation", ["loan_count"], ["branch", "format"]),
            _cube("fees", ["late_fees"], ["branch"]),
            _cube("member_loans", ["loan_count"], ["member_tier", "member_email"]),
            _view(
                "loans_view",
                [
                    _measure(
                        "loans_view", "circulation", "loan_count", "Loans", aliases=["checkouts"]
                    )
                ],
                [
                    _dim("loans_view", "circulation", "branch"),
                    _dim("loans_view", "circulation", "format"),
                ],
            ),
            _view(
                "fees_view",
                [_measure("fees_view", "fees", "late_fees", "Late fees")],
                [_dim("fees_view", "fees", "branch")],
            ),
            _view(
                "members_view",
                [_measure("members_view", "member_loans", "loan_count", "Loans")],
                [
                    _dim("members_view", "member_loans", "member_tier"),
                    _dim("members_view", "member_loans", "member_email"),
                ],
            ),
        ]
    }
)

OVERLAY = parse_overlay(
    """
version: 1
cubes:
  fees:
    common_questions: [late fees by branch last month]
lenses:
  lending: {views: [loans_view]}
  fees:
    views: [fees_view]
    not_for:
    - {ask: renewal penalties, sibling: lending}
    not_computable:
    - measure: fee waivers
      aliases: [waived fees]
      reason: waivers are not recorded
  members: {views: [members_view]}
definitions:
- metric: overdue
  about: fees
  summary: A charge past its due date.
  aliases: [past due]
hide:
- {cube: member_loans, dimension: member_email, except_groups: []}
scope:
  names:
    Harbour Libraries: [harbour, dictionary:circulation.branch]
"""
)

PROBE = CubeProbe.model_validate(
    {
        "dictionaries": {
            "circulation.branch": ["Harbour", "Mill"],
            "circulation.format": ["ebook", "print", "audio"],
            "fees.branch": ["Harbour", "Mill"],
            "member_loans.member_tier": ["student", "senior", "adult"],
        }
    }
)


def _profile(lens: str, **cfg: Any) -> CoverageProfile:
    config = LensConfig(name=lens, connections=["wh"], serving="cube", **cfg)
    model = LensModel.build(META, OVERLAY, lens)
    return cube_coverage_profile(config, model, PROBE, certified=cfg.pop("certified", None))


CATALOGUE = [_profile("lending"), _profile("fees"), _profile("members")]


class _Scripted:
    """A decider that records what it was shown and picks the named lens."""

    typed_decisions = False

    def __init__(self, lens: str | None = None) -> None:
        self.lens = lens
        self.calls: list[tuple[str, list[str]]] = []

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        names = [o.name for o in options]
        self.calls.append((context, names))
        chosen = self.lens if self.lens in names else names[0]
        return Decision(chosen=chosen, probs={chosen: 1.0}, provider="scripted")


def _route(question: str, decider: _Scripted) -> RouteDecision:
    return route_question(
        CATALOGUE, _LexicalEmbedder(), LlmDecider(None, "", decider=decider), question
    )


# ── the profile ──────────────────────────────────────────────────────────────


def test_the_profile_is_read_from_the_lens_model() -> None:
    loans = _profile("lending", certified=["loans by format in 2026"])
    assert loans.cube_lane
    assert "Loans" in loans.anchors and "loans by format in 2026" in loans.anchors
    assert loans.values["format"] == ["ebook", "print", "audio"]
    assert loans.vocabulary is not None and loans.declared is not None
    # meta.dst aliases, dictionaries and the overlay's company names are held.
    assert vocabulary(["checkouts", "ebook", "harbour", "mill"]) <= loans.declared
    fees = _profile("fees")
    assert fees.not_for == ["renewal penalties"]
    assert fees.declared is not None
    # The overlay's definitions, questions and not-computable terms are the lens's own.
    assert vocabulary(["overdue", "past due", "fee waivers", "waived"]) <= fees.declared
    assert "late fees by branch last month" in fees.anchors
    members = _profile("members")
    # A member hidden from everyone holds nothing.
    assert members.declared is not None and not vocabulary(["email"]) & members.declared
    assert "member_tier" in members.dimensions


# ── coverage first, the decider on a tie ─────────────────────────────────────


def test_one_lens_holding_the_words_routes_with_no_model_asked() -> None:
    decider = _Scripted()
    d = _route("late fees at Harbour last month", decider)
    assert (d.covered, d.lens, d.decided_by) == (True, "fees", "coverage")
    assert decider.calls == []


def test_lenses_tied_on_coverage_go_to_the_decider_with_the_whole_catalogue() -> None:
    decider = _Scripted("members")
    d = _route("loans last month", decider)
    assert (d.covered, d.lens, d.decided_by) == (True, "members", "decider")
    [(context, options)] = decider.calls
    assert sorted(options) == ["lending", "members"]
    # The context describes every lens the caller may use, the same text for
    # every question: the part the provider caches.
    assert all(f"- {p.lens}:" in context for p in CATALOGUE)
    assert d.fallbacks == ["lending"]


def test_a_lens_lacking_a_word_another_lens_stores_is_still_in_the_field() -> None:
    decider = _Scripted("lending")
    d = _route("checkouts for students this year", decider)
    # lending calls the figure 'checkouts' and stores no tier; members stores
    # 'student' and calls it loans. Each lacks one word: no decline, the
    # decider picks, and the lane's own word check judges the rest.
    assert (d.covered, d.lens) == (True, "lending")
    assert sorted(decider.calls[0][1]) == ["lending", "members"]


def test_a_question_whose_words_only_an_excluded_lens_holds_is_declined() -> None:
    decider = _Scripted()
    d = _route("renewal penalties waived", decider)
    # fees declares the ask is not its own; nobody else holds 'waived'.
    assert not d.covered and "nothing for 'waived'" in d.reason
    assert decider.calls == []


# ── the decider's prompt caches ─────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[CacheableBlock], list[Message]]] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.calls.append((system, messages))
        return LLMResult(text="fees")


def test_the_decision_context_rides_the_cached_system_block() -> None:
    llm = _Recorder()
    VoteDecider(llm, "m", k=1).decide(
        question="late fees?",
        context="THE CATALOGUE",
        options=[Option("fees"), Option("loans")],
        allow_none=True,
    )
    [(system, messages)] = llm.calls
    assert len(system) == 1 and system[0].cache and "THE CATALOGUE" in system[0].text
    assert "THE CATALOGUE" not in messages[0].content
    assert "- fees\n- loans" in messages[0].content and "late fees?" in messages[0].content


# ── the door: another lens's ask falls through ───────────────────────────────


def test_a_lens_saying_not_this_lens_is_passed_over() -> None:
    def refused(answer: str, mode: FailureMode | None) -> QueryResponse:
        return QueryResponse(
            lens="x", status="refused", answer=answer, mode=mode, request_id="req_x"
        )

    assert _refused_for_data(refused("this lens does not answer x; ask y", FailureMode.misrouted))
    assert _refused_for_data(
        refused("a.b is outside this lens's views", FailureMode.scope_violation)
    )
    assert _refused_for_data(
        refused(f"{LENS_DATA_REFUSAL}no member holds it", FailureMode.not_declared)
    )
    # A declared not-computable term is the governed answer: it stays.
    assert not _refused_for_data(refused("cannot compute fee waivers", FailureMode.not_declared))


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
        org = c.execute(text("INSERT INTO org (name) VALUES ('CubeRT') RETURNING id")).scalar_one()
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


def _bundle(name: str, not_for: list[dict[str, str]] | None = None) -> LensBundle:
    model = SemanticModel(lens=name, dialect="duckdb", entities=[], not_for=not_for or [])
    return LensBundle(config=LensConfig(name=name, connections=["wh"]), semantic_model=model)


PUBLISHED = [
    ("fees", "Fees", "", _bundle("fees", [{"ask": "renewal penalties", "sibling": "lending"}])),
    ("lending", "Lending", "", _bundle("lending")),
    ("members", "Members", "", _bundle("members")),
]


def _routed(lens: str, fallbacks: list[str] | None = None, covered: bool = True) -> RouteDecision:
    return RouteDecision(
        covered,
        lens if covered else None,
        0.8,
        [],
        "routed" if covered else "no governed lens covers this (llm)",
        decided_by="coverage",
        chain=[{"lens": lens, "result": "routed", "why": "covers every word of the question"}],
        fallbacks=fallbacks or [],
    )


def _ask(
    monkeypatch: pytest.MonkeyPatch,
    decision: RouteDecision,
    answers: dict[str, QueryResponse],
    q: str = "renewal penalties by branch",
) -> tuple[Any, list[str], Any]:
    org, raw = _org_token()
    asked: list[str] = []

    def fake_run(name: str, q: str, *a: object, **k: object) -> QueryResponse:
        asked.append(name)
        return answers[name]

    monkeypatch.setattr(route, "_route", lambda caller, q: decision)
    monkeypatch.setattr(route, "run_lens_query", fake_run)
    monkeypatch.setattr(route, "list_published_for_org", lambda org_id: PUBLISHED)
    monkeypatch.setattr(route, "lens_refusal_named", lambda q, caller: None)
    try:
        r = client.post(
            "/v1/query",
            json={"q": q},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        with create_engine(settings.database_admin_url).connect() as c:
            row = c.execute(
                text("SELECT routed_lens, decision FROM routing_decision WHERE org_id = :o"),
                {"o": org},
            ).one()
        return r.json(), asked, row
    finally:
        _cleanup(org)


OK = QueryResponse(lens="lending", answer="12 penalties", request_id="req_l")


@needs_db
def test_a_routed_lens_whose_not_for_names_the_ask_hands_it_to_the_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, asked, row = _ask(monkeypatch, _routed("fees"), {"lending": OK})
    assert body["routed_to"]["lens"] == "lending" and body["answer"]["answer"] == "12 penalties"
    assert asked == ["lending"]
    assert row[0] == "lending"
    assert [(c["lens"], c["result"]) for c in row[1]["chain"]][-2:] == [
        ("fees", "refused"),
        ("lending", "answered"),
    ]


@needs_db
def test_a_declined_question_a_lens_declares_another_lenses_goes_to_that_lens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, asked, row = _ask(monkeypatch, _routed("fees", covered=False), {"lending": OK})
    assert body["covered"] and body["routed_to"] == {
        "lens": "lending",
        "score": 0.0,
        "decided_by": "not_for",
    }
    assert asked == ["lending"] and row[0] == "lending"


@needs_db
def test_the_cube_lanes_not_for_refusal_falls_through_to_the_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The router sent it to members; its lane refuses the ask as fees' (the
    # sibling named in its own refusal is not published, so the fallbacks run).
    lane_refusal = QueryResponse(
        lens="members",
        status="refused",
        answer="this lens does not answer penalties; ask nowhere",
        mode=FailureMode.misrouted,
        request_id="req_m",
    )
    cannot = QueryResponse(
        lens="lending",
        status="refused",
        answer=f"{LENS_DATA_REFUSAL}the model holds no penalty",
        mode=FailureMode.not_declared,
        request_id="req_x",
    )
    fees = QueryResponse(lens="fees", answer="3 fees", request_id="req_f")
    body, asked, _row = _ask(
        monkeypatch,
        _routed("members", ["lending", "fees"]),
        {"members": lane_refusal, "lending": cannot, "fees": fees},
        q="penalties by branch",
    )
    assert asked == ["members", "lending", "fees"]
    assert body["routed_to"]["lens"] == "fees"


def test_a_lens_served_through_cube_is_routed_on_its_stored_lens_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.contracts.semantic_model import CubeStamp
    from services.governance.credentials import CallerIdentity

    stamp = CubeStamp(
        model_version="m",
        overlay_version="o",
        stamped_at="2026-09-01T00:00:00+00:00",
        views=["loans_view"],
        lens_model=LensModel.build(META, OVERLAY, "lending").model_dump(mode="json"),
    )
    cube = LensBundle(
        config=LensConfig(name="lending", connections=["wh"], serving="cube"),
        semantic_model=SemanticModel(lens="lending", dialect="duckdb", entities=[], cube=stamp),
    )
    monkeypatch.setattr(
        route, "list_published_for_org", lambda org: [("lending", "", "", cube), *PUBLISHED[:1]]
    )
    monkeypatch.setattr(route, "_certified_questions", lambda caller, lenses: {})
    monkeypatch.setattr(route, "_stored_profiles", lambda caller, bundle, seen: [])
    monkeypatch.setattr(route, "_cube_probe", lambda: PROBE)
    caller = CallerIdentity(org_id=uuid.uuid4(), name="t", is_admin=True)
    profiles, _known = route._accessible_profiles(caller)
    by = {p.lens: p for p in profiles}
    assert by["lending"].cube_lane and "Loans" in by["lending"].anchors
    assert by["lending"].values["format"] == ["ebook", "print", "audio"]
    # A lens on the typed core keeps its entity profile.
    assert not by["fees"].cube_lane
