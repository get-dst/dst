"""An ordinary word of the question never binds a stored value by coincidence.

The failure: "Which country has the most pro players?" compiled
``WHERE team_name = 'Most'``, because a team is named "Most" and value matching
was case-insensitive. The answer was a confident figure for one team's players,
served as the whole scene's.

Pinned here: a stored value spelled like a function or quantifier word ("most",
"top", "all") binds only where the question writes it as a name (capitalised
mid-sentence as stored, quoted, or possessive); sentence-initial capitals and
a type noun in front ("which team most often wins") are not enough. Multi-word
values and words with no ordinary reading keep matching as before.
"""

from __future__ import annotations

from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedResolver, named_values

TEAMS = {"team_name": ["Most", "Team Spirit", "Aurora Gaming", "Liquid", "Tundra Esports"]}
QUESTION = "Which country has the most pro players?"

MODEL = SemanticModel(
    lens="pro",
    dialect="duckdb",
    entities=[
        Entity(
            name="pro_players",
            source=EntitySource(connection="wh", table="pro_players"),
            fields=[
                Field(name="account_id", type="integer"),
                Field(name="country", type="string"),
                Field(name="team_name", type="string"),
            ],
            dimensions=[Dimension(name="country"), Dimension(name="team_name")],
            metrics=[Metric(name="players", agg="count_distinct", expr="pro_players.account_id")],
        )
    ],
)


class _Reads:
    """Reads the question the way a typed-decision model did: a ranking of
    players by country, no restriction asked for, and a stored value the
    question 'names' restricts the column that holds it."""

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        if "restrict" in context:
            named = "names '" in context
            chosen = next((n for n in names if n != "none"), None) if named else None
        else:
            chosen = next((p for p in ("ranking", "players", "country") if p in names), None)
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="t")


def test_the_most_binds_nothing_and_the_ranking_is_by_country() -> None:
    assert named_values(QUESTION, TEAMS) == []
    res = TypedResolver(_Reads(), domains=TEAMS).resolve(QUESTION, MODEL)  # type: ignore[arg-type]
    assert res.intent is not None, res.clarification
    assert res.intent.filters == [], res.intent.filters
    assert res.intent.dimensions == ["country"]
    assert "team_name" not in compile_intent(res.intent, MODEL)


def test_a_function_word_value_binds_only_where_the_question_writes_it_as_a_name() -> None:
    most = [{"team_name": "Most"}]
    # Capitalised mid-sentence, as stored: a name.
    assert named_values("How did Most do this patch?", TEAMS) == most
    assert named_values("How many matches did team Most win?", TEAMS) == most
    # Possessive or quoted: a name, whatever the case or position.
    assert named_values("Most's record this patch?", TEAMS) == most
    assert named_values('How many matches did "most" win?', TEAMS) == most
    # A sentence-initial capital is every word's, and a type noun in front
    # reads just as well as an adverb: neither makes the word a name.
    assert named_values("Most teams play how many matches?", TEAMS) == []
    assert named_values("Which team most often wins?", TEAMS) == []
    assert named_values("which team has the MOST wins?", TEAMS) == []


def test_names_with_no_ordinary_reading_match_as_before() -> None:
    assert named_values("How many matches has team spirit played?", TEAMS) == [
        {"team_name": "Team Spirit"}
    ]
    assert named_values("Did Aurora Gaming beat Team Spirit?", TEAMS) == [
        {"team_name": "Aurora Gaming"},
        {"team_name": "Team Spirit"},
    ]
    assert named_values("how did liquid do this patch?", TEAMS) == [{"team_name": "Liquid"}]


def test_a_lowercase_category_value_spelled_like_a_quantifier_needs_quoting() -> None:
    lanes = {"lane": ["top", "mid", "bot"]}
    assert named_values("top 5 heroes by win rate in the mid lane", lanes) == [{"lane": "mid"}]
    assert named_values("win rate in the 'top' lane", lanes) == [{"lane": "top"}]
