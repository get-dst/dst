"""A definition's SQL and the SQL that applies it are compared as trees, not text.

The failure: a correct answer graded partial, "this lens defines 'hero_win_rate'
in SQL, and the answer was computed without that definition", because the
compiled derived metric wraps every expanded sibling in parentheses and the
definition's text does not; and a definition written over COUNT(*) never
matched SQL counting the key. Compared as text the check also errs the other
way: `name = 'x'` is a substring of `team_name = 'x'`.

Pinned in both directions: the forms that mean the same (parentheses,
qualifiers, case, ANDed conditions in another order, COUNT(*) against a count of
a declared key column) apply the definition; the forms that do not (a count of a
column that can be null, a longer column holding the shorter one's name) do not.
The certified-answer bindings read the same comparison. A count of every row
belongs to one entity: over a fact joined to a dimension, COUNT(*) is the fact's
declared count and never also the dimension's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.certify.bindings import certified_bindings
from services.certify.store import CertifiedAnswer
from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
    SharedProvenance,
)
from services.evals.slot_lane import gold_for
from services.runtime.compiler import compile_intent
from services.runtime.resolution import attribute, from_intent, grade
from services.runtime.sql_canon import carries
from services.runtime.verification import _definition_applied

WIN_RATE_SQL = (
    "CASE WHEN COUNT(player_performances.match_id) >= 20 THEN SUM(CASE WHEN "
    "player_performances.is_win THEN 1 ELSE 0 END) * 1.0 / "
    "NULLIF(COUNT(player_performances.match_id), 0) END"
)


def _model(*definitions: Definition, provenance: dict[str, str] | None = None) -> SemanticModel:
    return SemanticModel(
        lens="pro",
        dialect="duckdb",
        entities=[
            Entity(
                name="player_performances",
                source=EntitySource(connection="wh", table="marts.fact_player_match"),
                primary_key=["match_id", "player_slot"],
                fields=[
                    Field(name="match_id", type="integer"),
                    Field(name="player_slot", type="integer"),
                    Field(name="hero_name", type="string"),
                    Field(name="team_name", type="string"),
                    Field(name="name", type="string"),
                    Field(name="region", type="string"),
                    Field(name="is_win", type="boolean"),
                ],
                dimensions=[Dimension(name="hero_name")],
                metrics=[
                    Metric(name="games", agg="count", expr="player_performances.match_id"),
                    Metric(
                        name="wins",
                        agg="sum",
                        expr="CASE WHEN player_performances.is_win THEN 1 ELSE 0 END",
                    ),
                    Metric(
                        name="win_rate",
                        type="derived",
                        expr="CASE WHEN {games} >= 20 THEN {wins} * 1.0 / NULLIF({games}, 0) END",
                    ),
                ],
            )
        ],
        definitions=list(definitions),
        shared_provenance=(
            SharedProvenance(compiled_at="2026-09-01T00:00:00Z", assets=provenance)
            if provenance
            else None
        ),
    )


def _applied(definition: Definition, sql: str) -> str:
    return _definition_applied("?", _model(definition), definition.term, sql).status


# ── the same meaning, written another way: applied ───────────────────────────


def test_the_compiled_derived_metric_applies_the_definition_it_implements() -> None:
    hero_win_rate = Definition(term="hero_win_rate", body="wins over games", sql_expr=WIN_RATE_SQL)
    sql = compile_intent(
        QueryIntent(entity="player_performances", metrics=["win_rate"], dimensions=["hero_name"]),
        _model(hero_win_rate),
    )
    assert "(COUNT(player_performances.match_id))" in sql  # the form that read as a departure
    assert _applied(hero_win_rate, sql) == "pass"


def test_count_star_is_a_count_of_a_declared_key_column() -> None:
    by_star = Definition(
        term="win_share",
        body="wins over rows",
        sql_expr="SUM(CASE WHEN player_performances.is_win THEN 1 ELSE 0 END) * 1.0 / COUNT(*)",
    )
    sql = (
        "SELECT p.hero_name, SUM(CASE WHEN p.is_win THEN 1 ELSE 0 END) * 1.0 / "
        "COUNT(p.match_id) AS win_share FROM marts.fact_player_match AS p GROUP BY p.hero_name"
    )
    assert _applied(by_star, sql) == "pass"


def test_conditions_in_another_order_still_apply_the_definition() -> None:
    eu_wins = Definition(
        term="eu_win",
        body="a win in the EU region",
        sql_expr="player_performances.region = 'EU' AND player_performances.is_win = TRUE",
    )
    sql = (
        "SELECT COUNT(*) FROM marts.fact_player_match AS player_performances WHERE "
        "player_performances.is_win = TRUE AND player_performances.hero_name = 'Lion' "
        "AND player_performances.region = 'EU'"
    )
    assert _applied(eu_wins, sql) == "pass"


# ── a different meaning that shares text: not applied ────────────────────────


def test_a_count_of_a_column_that_can_be_null_is_not_a_count_of_every_row() -> None:
    by_star = Definition(
        term="team_share",
        body="rows over all rows",
        sql_expr="COUNT(*)",
    )
    sql = "SELECT COUNT(p.team_name) FROM marts.fact_player_match AS p"
    assert _applied(by_star, sql) == "fail"


def test_a_longer_column_does_not_carry_the_shorter_ones_condition() -> None:
    named_x = Definition(
        term="named_x", body="rows named x", sql_expr="player_performances.name = 'x'"
    )
    sql = (
        "SELECT COUNT(*) FROM marts.fact_player_match AS player_performances "
        "WHERE player_performances.team_name = 'x'"
    )
    assert _applied(named_x, sql) == "fail"


# ── the certified bindings read the same comparison ──────────────────────────


def test_a_certified_answer_is_bound_to_the_definition_its_sql_implements() -> None:
    hero_win_rate = Definition(term="hero_win_rate", body="wins over games", sql_expr=WIN_RATE_SQL)
    model = _model(
        hero_win_rate,
        provenance={
            "entity/player_performances": "e1",
            "definition/hero_win_rate": "d1",
        },
    )
    sql = compile_intent(
        QueryIntent(entity="player_performances", metrics=["win_rate"], dimensions=["hero_name"]),
        model,
    )
    assert certified_bindings(sql, model).get("definition/hero_win_rate") == "d1"


# ── a count of every row belongs to the entity whose rows the query reads ────
#
# COUNT(*) counts rows of the query's grain, the FROM entity's. Over a join to a
# dimension it is that entity's declared key count, never also the joined
# dimension's, and a declared count that matched leaves no inferred count.

_MATCH_SQL = {
    "pro_matches": (
        "SELECT COUNT(*) AS match_count FROM marts.fact_match AS pro_matches "
        "JOIN marts.dim_patch AS patches ON patches.patch_id = pro_matches.patch_id "
        "WHERE patches.is_current = TRUE"
    ),
    "pub_matches": (
        "SELECT COUNT(*) AS match_count FROM marts.fact_pub_match AS pub_matches "
        "JOIN marts.dim_patch AS patches ON patches.patch_id = pub_matches.patch_id "
        "WHERE pub_matches.game_type = 'ranked_all_pick' AND patches.is_current = TRUE"
    ),
}
_FACT_TABLE = {"pro_matches": "marts.fact_match", "pub_matches": "marts.fact_pub_match"}


def _match_model(fact: str) -> SemanticModel:
    return SemanticModel(
        lens="meta",
        dialect="duckdb",
        entities=[
            Entity(
                name=fact,
                source=EntitySource(connection="wh", table=_FACT_TABLE[fact]),
                primary_key=["match_id"],
                fields=[
                    Field(name="match_id", type="integer"),
                    Field(name="patch_id", type="integer"),
                    Field(name="game_type", type="string"),
                ],
                metrics=[Metric(name="match_count", agg="count", expr=f"{fact}.match_id")],
            ),
            Entity(
                name="patches",
                source=EntitySource(connection="wh", table="marts.dim_patch"),
                primary_key=["patch_id"],
                fields=[
                    Field(name="patch_id", type="integer"),
                    Field(name="is_current", type="boolean"),
                ],
                metrics=[Metric(name="patch_count", agg="count", expr="patches.patch_id")],
            ),
        ],
        definitions=[
            Definition(
                term="current_patch", body="the patch in play", sql_expr="patches.is_current = TRUE"
            )
        ],
    )


@pytest.mark.parametrize(
    ("fact", "count"),
    [
        ("pro_matches", "COUNT(*)"),
        ("pub_matches", "COUNT(*)"),
        ("pro_matches", "COUNT(pro_matches.match_id)"),
    ],
)
def test_a_count_over_a_join_is_the_driving_entitys_count_only(fact: str, count: str) -> None:
    """The slots lane on a server with no stored gold: the certified SQL is
    attributed into slots, and the typed reading must resolve to the same names."""
    model = _match_model(fact)
    sql = _MATCH_SQL[fact].replace("COUNT(*)", count)
    answer = CertifiedAnswer("c1", "meta", "How many matches on the current patch?", sql, "t")
    gold = gold_for(answer, model, SimpleNamespace(_domains=None))
    assert gold is not None and gold.method == "attributed"
    measures = [(s.name, s.source) for s in gold.slots if s.kind == "metric"]
    assert measures == [("match_count", "declared")]
    assert [s.name for s in gold.slots if s.kind == "definition"] == ["current_patch"]

    typed = from_intent(
        QueryIntent(entity=fact, metrics=["match_count"], definitions=["current_patch"]),
        model,
        None,
    )
    assert grade(gold, typed) == ("passed", "")


def test_a_joined_dimensions_key_count_is_not_a_count_of_the_querys_rows() -> None:
    model = _match_model("pro_matches")
    sql = _MATCH_SQL["pro_matches"]
    assert carries(sql, "COUNT(pro_matches.match_id)", model) is True
    assert carries(sql, "COUNT(patches.patch_id)", model) is False
    # Read from the dimension, COUNT(*) is the dimension's key count.
    by_patch = "SELECT COUNT(*) FROM marts.dim_patch AS p WHERE p.is_current = TRUE"
    assert carries(by_patch, "COUNT(patches.patch_id)", model) is True
    assert carries(by_patch, "COUNT(pro_matches.match_id)", model) is False


def test_the_join_key_of_the_fact_is_not_the_dimensions_key() -> None:
    """`pro_matches.patch_id` and `patches.patch_id` share a name, and the join
    makes them equal row by row; counting the fact's column still is not the
    dimension's declared count."""
    model = _match_model("pro_matches")
    sql = _MATCH_SQL["pro_matches"].replace("COUNT(*)", "COUNT(pro_matches.patch_id)")
    assert carries(sql, "COUNT(patches.patch_id)", model) is False
    gold = attribute(sql, model)
    assert [(s.name, s.source) for s in gold.slots if s.kind == "metric"] == [
        ("COUNT(pro_matches.patch_id)", "inferred")
    ]
