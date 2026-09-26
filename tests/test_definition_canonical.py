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
The certified-answer bindings read the same comparison.
"""

from __future__ import annotations

from services.certify.bindings import certified_bindings
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
from services.runtime.compiler import compile_intent
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
