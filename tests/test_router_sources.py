"""A lens declares the systems its data comes from, and routing reads it.

Two lenses over one subject describe the same figures: one is fed by the
ticketing system, one by the door scanners, and "attendance from the door
scanners" scores alike against both. Which systems a lens reads is a fact
the author declares (``sources`` in lens.yaml) and the router applies word
for word: a question naming a declared system goes only to the lenses that
declare it, and one naming a system no lens the caller may use declares is
uncovered, saying which system.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from services.api import route
from services.api.route import _LexicalEmbedder
from services.contracts.lens_config import AccessConfig, AccessRule, LensConfig
from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.governance.credentials import CallerIdentity
from services.lenses.store import LensBundle
from services.router import CoverageProfile
from services.router.decider import LlmDecider, _options, route_question
from services.router.profiles import coverage_profile
from services.router.sibling import by_sources, named_sources
from services.validate.report import validate_bundle


class _TopPick:
    typed_decisions = False

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        return Decision(chosen=options[0].name, probs={options[0].name: 1.0}, provider="scripted")


SOLD = CoverageProfile(
    "attendance_sold",
    ["attendance per show", "tickets sold per show"],
    sources=["ticketing"],
)
SCANNED = CoverageProfile(
    "attendance_scanned",
    ["door scans per show"],
    sources=["door scanners", "turnstile log"],
)
PLAIN = CoverageProfile("show_kpis", ["attendance this season"])
LENSES = [SOLD, SCANNED, PLAIN]


def _route(question: str, *, known: list[str] | None = None) -> object:
    return route_question(
        LENSES,
        _LexicalEmbedder(),
        LlmDecider(None, "", decider=_TopPick()),
        question,
        known_sources=known,
    )


def test_similarity_alone_sends_the_named_system_to_the_wrong_lens() -> None:
    plain = [CoverageProfile(p.lens, p.anchors) for p in LENSES]
    d = route_question(
        plain,
        _LexicalEmbedder(),
        LlmDecider(None, "", decider=_TopPick()),
        "attendance per show from the door scanners",
    )
    assert d.lens == "attendance_sold"


@pytest.mark.parametrize(
    "question",
    [
        "attendance per show from the door scanners",
        "Attendance per show (Door-Scanners)",
        "attendance per show in the turnstile_log",
    ],
)
def test_a_named_system_routes_to_the_lens_declaring_it(question: str) -> None:
    d = _route(question)
    assert d.lens == "attendance_scanned", d.reason


def test_a_question_naming_no_system_routes_as_before() -> None:
    assert _route("attendance per show").lens == "attendance_sold"


def test_a_system_no_usable_lens_declares_is_uncovered_with_that_reason() -> None:
    d = _route("attendance per show from the loyalty app", known=["ticketing", "loyalty app"])
    assert not d.covered
    assert d.reason == "the question names loyalty app, and no lens you can use reads it"


def test_two_systems_need_a_lens_reading_both() -> None:
    d = _route("attendance per show from ticketing and the door scanners")
    assert not d.covered
    assert "no lens you can use reads them together" in d.reason


def test_matching_is_word_bounded_and_longest_first() -> None:
    known = ["door scanners", "scanners", "ticketing"]
    assert named_sources("door scanners last week", known) == ["door scanners"]
    assert named_sources("scanners last week", known) == ["scanners"]
    # Inside a longer word is not the word.
    assert named_sources("e-ticketing fees", known) == ["ticketing"]
    assert named_sources("ticketingfees", known) == []
    kept, why = by_sources("tickets sold per show", LENSES, ["ticketing"])
    assert kept == LENSES and why is None


def test_the_decider_reads_sources_and_scope() -> None:
    market = CoverageProfile("market_share", ["share of city attendance"], data_scope="market")
    shown = {o.name: o.description for o in _options([SCANNED, market])}
    assert "Reads: door scanners, turnstile log." in shown["attendance_scanned"]
    assert "Figures: the market's, or other organisations'." in shown["market_share"]


def _model() -> SemanticModel:
    return SemanticModel(
        lens="attendance_scanned",
        dialect="duckdb",
        entities=[
            Entity(
                name="scans",
                source=EntitySource(connection="wh", table="scans"),
                fields=[Field(name="scan_id", type="string")],
                metrics=[
                    Metric(name="door_scans", agg="count", expr="scans.scan_id", label="Door scans")
                ],
            )
        ],
    )


def test_the_profile_carries_what_the_lens_declares() -> None:
    cfg = LensConfig(
        name="attendance_scanned",
        connections=["wh"],
        sources=["door scanners"],
        scope="own",
    )
    prof = coverage_profile(LensBundle(config=cfg, semantic_model=_model()), certified=[])
    assert (prof.sources, prof.data_scope) == (["door scanners"], "own")


def test_the_config_rejects_what_can_match_nothing() -> None:
    with pytest.raises(ValidationError, match="declared twice"):
        LensConfig(name="a", sources=["door_scanners", "Door scanners"])
    with pytest.raises(ValidationError, match="names no system"):
        LensConfig(name="a", sources=["--"])
    with pytest.raises(ValidationError):
        LensConfig(name="a", scope="everyone")
    assert LensConfig(name="a", sources=["  door   scanners "]).sources == ["door scanners"]


def test_plan_warns_on_a_source_spelled_like_a_metric() -> None:
    cfg = LensConfig(name="attendance_scanned", connections=["wh"], sources=["door scans"])
    issues = validate_bundle(LensBundle(config=cfg, semantic_model=_model()), [], []).issues
    hit = [i for i in issues if i.code == "source_names_a_metric"]
    assert hit and hit[0].severity == "warning" and "door_scans" in hit[0].message


def test_the_api_knows_systems_of_lenses_the_caller_cannot_use(monkeypatch) -> None:  # noqa: ANN001
    open_cfg = LensConfig(
        name="attendance_sold",
        connections=["wh"],
        sources=["ticketing"],
        access=AccessConfig(allow=[AccessRule(group="everyone")]),
    )
    closed_cfg = LensConfig(name="patrons", connections=["wh"], sources=["loyalty app"])
    bundles = [
        (c.name, c.name, "", LensBundle(config=c, semantic_model=_model()))
        for c in (open_cfg, closed_cfg)
    ]
    monkeypatch.setattr(route, "list_published_for_org", lambda org: bundles)
    monkeypatch.setattr(route, "_certified_questions", lambda caller, lenses: {})
    caller = CallerIdentity(org_id=uuid.uuid4(), name="analyst", is_admin=False)
    profiles, known = route._accessible_profiles(caller)
    assert [p.lens for p in profiles] == ["attendance_sold"]
    assert sorted(known) == ["loyalty app", "ticketing"]
    monkeypatch.setattr(route, "_resolve_decider", lambda: None)
    monkeypatch.setattr(route, "_resolve_embedder", lambda: _LexicalEmbedder())
    d = route._route(caller, "door scans from the loyalty app")
    assert not d.covered and "loyalty app" in d.reason
