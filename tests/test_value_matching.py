"""Stored values the question names are matched, not decided.

A counted text column small enough to hold whole (MATCH_DICTIONARY_MAX) gets a
complete dictionary; the typed resolver reads the values a question names
verbatim off it — word-bounded, longest first, possessives included — and
fixes the filter value without a decision. Two named values over two columns
become two filters, one per column; a named value no filter uses clarifies.
Dictionaries past the enum cap never reach text a model reads.
"""

from __future__ import annotations

from typing import Any

import duckdb

from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedDecider
from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    ColumnProfile,
    ColumnSampleSpec,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.lenses import profile_enrich
from services.runtime import value_guard
from services.runtime.generator import serialize_model
from services.runtime.option_sets import option_sets
from services.runtime.typed_resolver import TypedResolver, named_values
from services.semantic.introspect import _column_facts

HEROES = [f"Hero {i:03d}" for i in range(125)] + ["Crystal Maiden", "Lion"]


def _duos() -> SemanticModel:
    return SemanticModel(
        lens="dota",
        dialect="duckdb",
        entities=[
            Entity(
                name="pub_duos",
                source=EntitySource(connection="wh", table="pub_duos"),
                fields=[
                    Field(name="hero_name", type="string"),
                    Field(name="ally_hero_name", type="string"),
                    Field(name="games", type="integer"),
                    Field(name="wins", type="integer"),
                ],
                metrics=[Metric(name="duo_win_rate", agg="avg", expr="pub_duos.wins")],
            )
        ],
    )


OWN = {"pub_duos": {"hero_name": HEROES, "ally_hero_name": HEROES}}


def _sure(chosen: str | None) -> Decision:
    return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")


def _resolver(steps: list[Decision]) -> tuple[TypedResolver, ScriptedDecider]:
    decider = ScriptedDecider(steps)
    return TypedResolver(decider, entity_domains=OWN), decider


# ── the matcher ─────────────────────────────────────────────────────────────


def test_a_possessive_names_the_value() -> None:
    domains = {"hero_name": HEROES}
    for q in ("What is Crystal Maiden's win rate?", "crystal maiden’s games"):
        assert named_values(q, domains) == [{"hero_name": "Crystal Maiden"}]


def test_the_longest_value_wins_and_a_word_is_never_matched_inside_another() -> None:
    domains = {"hero_name": ["Maiden", "Crystal Maiden", "Lion"]}
    assert named_values("Crystal Maiden with Lion", domains) == [
        {"hero_name": "Crystal Maiden"},
        {"hero_name": "Lion"},
    ]
    assert named_values("how do Lions fare", domains) == []  # "Lions" is not "Lion"


def test_a_value_held_by_two_columns_is_one_named_value() -> None:
    got = named_values("win rate with Lion", {"hero_name": HEROES, "ally_hero_name": HEROES})
    assert got == [{"hero_name": "Lion", "ally_hero_name": "Lion"}]


def test_a_short_code_matches_only_as_written_and_a_number_never() -> None:
    domains = {"country": ["IN", "FI", "NO"], "season": ["2024", "2025"]}
    assert named_values("sales in Finland, no returns, in 2024", domains) == []
    assert named_values("sales in IN and FI", domains) == [{"country": "IN"}, {"country": "FI"}]


# ── the resolver ────────────────────────────────────────────────────────────


def test_two_named_values_over_two_columns_are_two_filters() -> None:
    """The measured defect: the first picked column clarified for a binding and
    the second filter was never reached. Now the value is read off the text —
    the only decision left is which of the two named values is the ally."""
    steps = [
        _sure("aggregate"),
        _sure("duo_win_rate"),
        _sure("ally_hero_name"),  # filter:0
        _sure("Lion"),  # which named value is the ally
        _sure("hero_name"),  # filter:1 — its value is the one left, no decision
        _sure(None),  # filter:2
    ]
    resolver, decider = _resolver(steps)
    res = resolver.resolve("What is Crystal Maiden's win rate with Lion?", _duos())
    assert res.intent is not None, res.clarification
    assert {(f.field, f.op, f.value) for f in res.intent.filters} == {
        ("ally_hero_name", "=", "Lion"),
        ("hero_name", "=", "Crystal Maiden"),
    }
    # the value decision offered the two named values, never the dictionary
    assert ["Crystal Maiden", "Lion"] in [c["options"] for c in decider.calls]
    assert all(len(c["options"]) <= LOW_CARDINALITY_MAX + 2 for c in decider.calls)  # type: ignore[arg-type]
    read = [d for d in res.decisions if d.provider == "question-text"]
    assert [(d.slot, d.chosen, d.p) for d in read] == [("filter_value", "Crystal Maiden", None)]
    assert res.matched == ["ally_hero_name", "hero_name"]


def test_a_value_in_two_columns_leaves_the_column_a_decision_and_the_value_fixed() -> None:
    steps = [_sure("aggregate"), _sure("duo_win_rate"), _sure("hero_name"), _sure(None)]
    resolver, decider = _resolver(steps)
    res = resolver.resolve("Lion win rate", _duos())
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters] == [("hero_name", "Lion")]
    assert len(decider.calls) == 4  # shape, metric, filter:0, filter:1 — no value asked


def test_a_named_value_no_filter_uses_clarifies_never_drops() -> None:
    steps = [_sure("aggregate"), _sure("duo_win_rate"), _sure(None)]
    resolver, _ = _resolver(steps)
    res = resolver.resolve("What is Crystal Maiden's win rate?", _duos())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "Crystal Maiden"
    assert res.clarification.options == ["ally_hero_name", "hero_name"]


def test_a_long_dictionary_with_nothing_named_asks_for_a_binding_not_a_pick() -> None:
    steps = [_sure("aggregate"), _sure("duo_win_rate"), _sure("hero_name"), _sure("stated")]
    resolver, decider = _resolver(steps)
    res = resolver.resolve("What is CM's win rate?", _duos())
    assert res.clarification is not None and res.clarification.term == "hero_name"
    assert all(len(c["options"]) <= LOW_CARDINALITY_MAX + 2 for c in decider.calls)  # type: ignore[arg-type]


# ── no prompt growth ────────────────────────────────────────────────────────


def _long() -> ColumnProfile:
    return ColumnProfile(
        name="hero_name",
        type="VARCHAR",
        distinct_count=len(HEROES),
        distinct_is_exact=True,
        top_values=HEROES,
        values_complete=True,
    )


def test_a_long_dictionary_never_reaches_a_prompt() -> None:
    model = _duos()
    prof = TableProfile(connection="wh", table="pub_duos", columns=[_long()])
    prompt = serialize_model(profile_enrich.enrich_model(model, [prof]))
    assert "Hero 000" not in prompt and "distinct: 127" in prompt
    assert "Hero 000" not in _column_facts(_long())
    sets = option_sets(model, [], domains={"hero_name": HEROES, "tier": ["a", "b"]})
    assert "filter_value:hero_name" not in sets and "filter_value:tier" in sets
    miss = value_guard.UnknownLiteral("hero_name", "crystal maiden", tuple(HEROES))
    feedback = value_guard.repair_feedback([miss], "SELECT 1")
    assert "'Crystal Maiden'" in feedback and feedback.count("'Hero ") <= LOW_CARDINALITY_MAX
    clar = value_guard.unknown_value_clarification("hero_name", ("crystal maiden",), miss.values)
    assert clar.options[0] == "Crystal Maiden" and len(clar.options) <= LOW_CARDINALITY_MAX


# ── the profiler collects it ────────────────────────────────────────────────


def test_a_counted_text_column_up_to_the_match_cap_gets_its_whole_dictionary(
    tmp_path: Any,
) -> None:
    db_path = str(tmp_path / "heroes.duckdb")
    scratch = duckdb.connect(db_path)
    scratch.execute(
        "CREATE TABLE duos AS SELECT 'hero ' || (i % 127) AS hero_name, "
        "'player ' || (i % 600) AS player_name, "
        "'user' || (i % 127) || '@example.com' AS contact_email FROM range(1200) AS t(i)"
    )
    scratch.close()
    spec = TableSampleSpec(
        table="duos",
        row_count=1200,
        columns=[
            ColumnSampleSpec(name="hero_name", type="VARCHAR", matchable=True),
            ColumnSampleSpec(name="player_name", type="VARCHAR", matchable=True),
            # same size as hero_name, but not a declared dimension: never listed
            ColumnSampleSpec(name="contact_email", type="VARCHAR"),
        ],
    )
    [profile] = DuckDBConnector(db_path).sample_profile([spec])
    heroes = next(c for c in profile.columns if c.name == "hero_name")
    assert heroes.values_complete and not heroes.is_low_cardinality
    assert heroes.top_values is not None
    assert sorted(heroes.top_values) == sorted(f"hero {i}" for i in range(127))
    players = next(c for c in profile.columns if c.name == "player_name")
    assert players.distinct_count == 600
    assert not players.values_complete and players.top_values is None
    emails = next(c for c in profile.columns if c.name == "contact_email")
    assert emails.distinct_count == 127
    assert not emails.values_complete and emails.top_values is None
