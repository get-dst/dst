"""A lens owns the refusal of what it declares it cannot compute.

A question naming a ``not_computable`` measure (or one of its aliases) routes to
the lens that declares it, on both lanes, so that lens's own refusal is the
answer: before, the term was no routing anchor, a sibling scored closest and
served a different figure under the asked name. A ``route_to`` naming a lens
the caller may use sends the question there instead, one hop. A lens that
declares the phrase as a metric of its own outranks a refusal.

A member's declared aliases are scored too: a question phrased only in an
alias one lens alone declares routes to it; a name or label outranks an alias.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.api.route import _LexicalEmbedder
from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.lens_config import LensConfig, NotComputable
from services.contracts.lens_model import LensModel, parse_overlay
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.router import CoverageProfile
from services.router.decider import route_question
from services.router.profiles import coverage_profile, cube_coverage_profile
from services.router.sibling import metric_name_owner, refusal_owner

# ── typed lane: a ferry line ─────────────────────────────────────────────────


def _typed(lens: str, entity: str, metrics: list[Metric], **cfg: Any) -> CoverageProfile:
    sm = SemanticModel(
        lens=lens,
        dialect="duckdb",
        entities=[
            Entity(
                name=entity,
                source=EntitySource(connection="wh", table=entity),
                fields=[Field(name="amount", type="number"), Field(name="route", type="string")],
                metrics=metrics,
            )
        ],
        not_computable=[nc.model_dump(mode="json") for nc in cfg.get("not_computable", [])],
    )
    config = SimpleNamespace(
        name=lens,
        display_name="",
        description="",
        model=SimpleNamespace(certified_dir=None),
        not_computable=list(cfg.get("not_computable", [])),
        not_for=[],
    )
    return coverage_profile(SimpleNamespace(config=config, semantic_model=sm), certified=[])  # type: ignore[arg-type]


CROSSINGS = _typed(
    "crossings",
    "crossings",
    [
        Metric(name="crossings_made", agg="count", expr="crossings.amount", label="Crossings made"),
        Metric(
            name="occupancy_ratio",
            agg="avg",
            expr="crossings.amount",
            label="Occupancy ratio",
            aliases=["load factor"],
        ),
    ],
    not_computable=[
        NotComputable(
            measure="no-show rate",
            aliases=["missed sailing rate"],
            reason="no-shows are not recorded per crossing",
        )
    ],
)
BOOKINGS = _typed(
    "bookings",
    "bookings",
    [
        Metric(
            name="cancellation_ratio", agg="avg", expr="bookings.amount", label="Cancellation ratio"
        ),
        Metric(name="bookings_made", agg="count", expr="bookings.amount", label="Bookings made"),
    ],
)
NO_SHOWS = _typed(
    "no_shows",
    "no_shows",
    [Metric(name="no_show_rate", agg="avg", expr="no_shows.amount", label="No-show rate")],
)


def _route(question: str, profiles: list[CoverageProfile]):
    return route_question(profiles, _LexicalEmbedder(), None, question)


def test_the_profile_carries_what_the_lens_refuses() -> None:
    assert CROSSINGS.refuses == {"no-show rate": "", "missed sailing rate": ""}
    assert "no-show rate" in CROSSINGS.anchors and "missed sailing rate" in CROSSINGS.anchors


def test_a_question_naming_a_refused_term_routes_to_the_lens_that_refuses_it() -> None:
    for question in ("What was the no-show rate in March?", "missed sailing rate by route"):
        decision = _route(question, [CROSSINGS, BOOKINGS])
        assert decision.covered and decision.lens == "crossings", (question, decision)
        assert decision.decided_by == "not_computable"
        assert decision.fallbacks == []


def test_a_lens_whose_metric_is_the_term_outranks_the_refusal() -> None:
    decision = _route("What was the no-show rate in March?", [CROSSINGS, BOOKINGS, NO_SHOWS])
    assert decision.covered and decision.lens == "no_shows"


def test_route_to_sends_the_question_one_hop_on() -> None:
    routed = _typed(
        "crossings_routed",
        "crossings",
        [Metric(name="crossings_made", agg="count", expr="crossings.amount")],
        not_computable=[NotComputable(measure="no-show rate", route_to="bookings")],
    )
    decision = _route("no-show rate last week", [routed, BOOKINGS])
    assert decision.covered and decision.lens == "bookings"
    assert decision.decided_by == "not_computable"
    # The lens it routes to is not one the caller may use: the refusal is the answer.
    decision = _route("no-show rate last week", [routed, NO_SHOWS.__class__(lens="x", anchors=[])])
    assert decision.covered and decision.lens == "crossings_routed"


def test_refusal_owner_reads_the_longest_term_named() -> None:
    assert refusal_owner("missed sailing rate by route", [CROSSINGS, BOOKINGS]) == "crossings"
    assert refusal_owner("crossings made by route", [CROSSINGS, BOOKINGS]) is None


# ── aliases are scored ───────────────────────────────────────────────────────


def test_an_alias_one_lens_alone_declares_routes_to_it() -> None:
    assert "load factor" in CROSSINGS.anchors
    assert metric_name_owner("load factor by route", [CROSSINGS, BOOKINGS]) == "crossings"
    decision = _route("load factor by route", [CROSSINGS, BOOKINGS])
    assert decision.covered and decision.lens == "crossings"


def test_a_name_outranks_an_alias() -> None:
    named = _typed(
        "loads",
        "loads",
        [Metric(name="load_factor", agg="avg", expr="loads.amount", label="Load factor")],
    )
    assert metric_name_owner("load factor by route", [CROSSINGS, named]) == "loads"


# ── the Cube lane: a library network ─────────────────────────────────────────


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


def _dim(view: str, cube: str, name: str) -> dict[str, Any]:
    return {
        "name": f"{view}.{name}",
        "aliasMember": f"{cube}.{name}",
        "title": name,
        "type": "string",
    }


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
            _cube("circulation", ["loan_count"], ["branch"]),
            _cube("fees", ["late_fees"], ["branch"]),
            {
                "name": "loans_view",
                "type": "view",
                "measures": [
                    _measure(
                        "loans_view", "circulation", "loan_count", "Loans", aliases=["checkouts"]
                    )
                ],
                "dimensions": [_dim("loans_view", "circulation", "branch")],
                "segments": [],
            },
            {
                "name": "fees_view",
                "type": "view",
                "measures": [_measure("fees_view", "fees", "late_fees", "Late fees")],
                "dimensions": [_dim("fees_view", "fees", "branch")],
                "segments": [],
            },
        ]
    }
)

OVERLAY = parse_overlay(
    """
version: 1
lenses:
  lending: {views: [loans_view]}
  fees:
    views: [fees_view]
    not_computable:
    - measure: fee waivers
      aliases: [waived fees]
      reason: waivers are not recorded
"""
)

PROBE = CubeProbe.model_validate(
    {"dictionaries": {"circulation.branch": ["Harbour", "Mill"], "fees.branch": ["Harbour"]}}
)


def _cube_profile(lens: str) -> CoverageProfile:
    config = LensConfig(name=lens, connections=["wh"], serving="cube")
    return cube_coverage_profile(config, LensModel.build(META, OVERLAY, lens), PROBE)


def test_the_cube_profile_scores_refused_terms_and_aliases() -> None:
    fees, lending = _cube_profile("fees"), _cube_profile("lending")
    assert fees.refuses == {"fee waivers": "", "waived fees": ""}
    assert {"fee waivers", "waived fees"} <= set(fees.anchors)
    assert "checkouts" in lending.anchors
    assert "checkouts" in lending.metric_aliases


def test_the_cube_lane_routes_a_refused_term_to_the_lens_that_refuses_it() -> None:
    catalogue = [_cube_profile("lending"), _cube_profile("fees")]
    for question in ("waived fees by branch", "How many fee waivers at Harbour?"):
        decision = _route(question, catalogue)
        assert decision.covered and decision.lens == "fees", (question, decision)
        assert decision.decided_by == "not_computable"


def test_the_cube_lane_routes_an_alias_to_the_lens_that_declares_it() -> None:
    decision = _route("checkouts by branch", [_cube_profile("lending"), _cube_profile("fees")])
    assert decision.covered and decision.lens == "lending"
