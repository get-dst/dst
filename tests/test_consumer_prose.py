"""The consumer's sentence — plain words for the person a public demo serves.

Under `demo.audience: consumer` the envelope lost its trust fields, and the
sentence inside it still spoke the system's language: "per the hero_win_rate
definition (wins ÷ games, shown only where games ≥ 20)", "(data as of
2026-09-26)", "the data is stale after 2 days per this lens", "the NULL rows
are carries that did not meet the query's 10-game threshold". The composer is
told those things so it will not invent them; the consumer prompt tells it the
same facts and a different register: figures as figures, the scope once in one
plain clause derived from the declared population, no metric, column,
definition, lens, window or table named, a decline in plain words. The
engineer prompt is untouched, the pipeline's own lines (the date, a truncation,
a decline) switch register with it, and the reconcile that keeps prose a
rendering of the rows grades both the same.
"""

from __future__ import annotations

import datetime
import re

from services.contracts.fakes import FakeConnector
from services.contracts.protocols import CacheableBlock, GeneratedQuery, LLMResult, Message
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.runtime import faithfulness
from services.runtime.answer import (
    _SYSTEM,
    AnswerComposer,
    compose_prompt,
    grounding_notes,
    spoken_date,
)
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query

_POPULATION = (
    "ranked All Pick public matchmaking games at each skill bracket (the newest matches "
    "OpenDota lists at each load, loaded since September 2025); Turbo and unranked games "
    "are excluded"
)
_DEFINITION = "wins ÷ games, shown only where games ≥ 20"


def _model() -> SemanticModel:
    return SemanticModel(
        lens="dota",
        dialect="duckdb",
        timezone="UTC",
        stale_after_days=2,
        entities=[
            Entity(
                name="hero_bracket_stats",
                source=EntitySource(connection="wh", table="hero_bracket_stats"),
                population=_POPULATION,
                fields=[
                    Field(name="hero_name", type="string"),
                    Field(name="bracket", type="string"),
                    Field(name="games", type="integer"),
                    Field(name="wins", type="integer"),
                ],
                dimensions=[Dimension(name="hero_name"), Dimension(name="bracket")],
                metrics=[
                    Metric(name="games", agg="sum", expr="hero_bracket_stats.games"),
                    Metric(name="wins", agg="sum", expr="hero_bracket_stats.wins"),
                    Metric(
                        name="hero_win_rate",
                        type="ratio",
                        numerator="wins",
                        denominator="games",
                        format="percent",
                        description=_DEFINITION,
                    ),
                ],
            )
        ],
        definitions=[
            Definition(
                term="hero_win_rate", about="hero_bracket_stats.hero_win_rate", body=_DEFINITION
            )
        ],
    )


_SQL = (
    "SELECT hero_name, SUM(wins) * 1.0 / SUM(games) AS hero_win_rate FROM hero_bracket_stats "
    "WHERE hero_name = 'Anti-Mage' AND bracket = 'Divine' GROUP BY hero_name"
)
_RESULT = QueryResult(columns=["hero_name", "hero_win_rate"], rows=[["Anti-Mage", 0.48876]])
_QUESTION = "What is Anti-Mage's win rate in the Divine bracket?"

# What must never reach a consumer's sentence: an identifier, or the system's
# words for its own machinery.
_IDENTIFIER = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
_MACHINERY = re.compile(r"\b(?:definition|lens|window|NULL|threshold)\b", re.IGNORECASE)


def _prompt(audience: str) -> tuple[str, str]:
    return compose_prompt(
        question=_QUESTION,
        generated=GeneratedQuery(sql=_SQL, definition_used="hero_win_rate"),
        result=_RESULT,
        semantic_model=_model(),
        audience=audience,  # type: ignore[arg-type]
    )


def test_the_consumer_prompt_carries_the_plain_rules_and_the_same_facts() -> None:
    system, user = _prompt("consumer")
    # the register
    assert "never name a metric, a column, a definition" in system
    assert "in one plain clause a person would say" in user
    assert "never quote it" in user
    # …over the same facts: the scope is the declared population and nothing else
    assert _POPULATION in user
    assert "Never say how current, fresh or old the data is" in user
    assert "stale after 2 days" not in user
    # every guarantee the engineer prompt states is stated here too
    for rule in (
        "never invent numbers",
        "copying the digits from the result exactly",
        "never write a sentence a definition contradicts",
        "NO_ANSWER:",
        "never NO_ANSWER",
    ):
        assert rule in system, rule
    # the decline comes in plain words too
    assert "naming no table or column" in system
    # the inputs are the same rows the engineer reads
    assert "Anti-Mage | 48.9%" in user


def test_the_engineer_prompt_is_unchanged_by_the_switch() -> None:
    default_system, default_user = compose_prompt(
        question=_QUESTION,
        generated=GeneratedQuery(sql=_SQL, definition_used="hero_win_rate"),
        result=_RESULT,
        semantic_model=_model(),
    )
    system, user = _prompt("engineer")
    assert (system, user) == (default_system, default_user)
    assert system == _SYSTEM
    assert "If a business definition was applied, mention it." in system
    assert "per the table profile" in system
    assert f"The data covers ONLY {_POPULATION} — state this scope in the answer" in user
    assert "This lens declares data stale after 2 days" in user
    # the two user turns differ only in the instruction blocks that switched
    consumer_user = _prompt("consumer")[1]
    for line in ("Question:", "SQL:", "Definition applied: hero_win_rate", "Anti-Mage | 48.9%"):
        assert line in user and line in consumer_user


class _Composer:
    """Returns the prose it was given and keeps the prompt it was sent."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.system = ""
        self.user = ""

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.system = "\n".join(b.text for b in system)
        self.user = "\n".join(m.content for m in messages)
        return LLMResult(text=self._text, input_tokens=1, output_tokens=1)


_ENGINEER_PROSE = (
    "Anti-Mage's win rate in the Divine bracket is 48.9% (per the hero_win_rate definition: "
    "wins ÷ games, shown only where games ≥ 20). This covers ranked All Pick only, from a "
    "sample of public matchmaking games; Turbo and unranked games are excluded."
)
_CONSUMER_PROSE = "Anti-Mage wins 48.9% of ranked public matches at Divine."


def _serve(audience: str, text: str) -> tuple[str, _Composer]:
    llm = _Composer(text)
    res = run_query(
        question=_QUESTION,
        lens_name="dota",
        org_id="org-1",
        caller="visitor",
        semantic_model=_model(),
        connector=FakeConnector(result=_RESULT),
        generator=FixedSQLGenerator(_SQL, definition_used="hero_win_rate"),
        composer=AnswerComposer(llm),
        data_as_of="2026-09-26",
        audience=audience,  # type: ignore[arg-type]
    )
    assert res.trace.status == "ok", res.trace.error
    return res.response.answer, llm


def test_a_consumer_serve_composes_in_the_plain_register_and_dates_as_a_person_writes() -> None:
    answer, llm = _serve("consumer", _CONSUMER_PROSE)
    assert "never name a metric, a column, a definition" in llm.system
    assert answer == f"{_CONSUMER_PROSE} (as of {spoken_date('2026-09-26')})"
    assert "data as of" not in answer and "2026-09-26" not in answer
    assert not _IDENTIFIER.search(answer), answer
    assert not _MACHINERY.search(answer), answer


def test_an_engineer_serve_is_the_answer_as_it_always_was() -> None:
    answer, llm = _serve("engineer", _ENGINEER_PROSE)
    assert llm.system == _SYSTEM
    assert answer == f"{_ENGINEER_PROSE} (data as of 2026-09-26)"
    assert _IDENTIFIER.search(answer) and _MACHINERY.search(answer)


def test_the_reconcile_grades_consumer_prose_the_same() -> None:
    """A plain sentence is a rendering of the rows like any other: a rounded
    figure is rewritten to the cell's own presentation, and the words a person
    writes around it — a day, a month, a year in the scope clause — are never
    'corrected' into a cell they resemble. The year comes from the declared
    population the composer was told to say in words, so the numeric gate
    grounds it there (`grounding_notes`) — the rail that kept a scope with a
    date in it from ever being said."""
    result = QueryResult(
        columns=["hero_name", "hero_win_rate", "games"], rows=[["Anti-Mage", 0.48876, 26]]
    )
    prose = (
        "Anti-Mage wins 48.88% of ranked public matches at Divine since September 2025, "
        "over 26 games (as of 26 September)."
    )
    notes = grounding_notes(_model(), result.columns, _SQL)
    assert "since September 2025" in notes
    out = faithfulness.reconcile(prose, result, question_text=_QUESTION, notes_text=notes)
    assert "48.9%" in out and "48.88" not in out
    assert "since September 2025" in out and "over 26 games (as of 26 September)" in out
    assert faithfulness.numeric_check(out, result, question_text=_QUESTION, notes_text=notes) == (
        "pass",
        None,
    )
    # …and without the scope on the grounding rail the same sentence is withheld
    assert faithfulness.numeric_check(out, result, question_text=_QUESTION)[0] == "fail"


def test_the_consumer_hears_a_truncation_in_words_for_a_person() -> None:
    rows = [[f"hero {i}", 0.5] for i in range(300)]
    res = run_query(
        question="list every hero's win rate",
        lens_name="dota",
        org_id="org-1",
        caller="visitor",
        semantic_model=_model(),
        connector=FakeConnector(
            result=QueryResult(columns=["hero_name", "hero_win_rate"], rows=rows)
        ),
        generator=FixedSQLGenerator("SELECT hero_name, hero_win_rate FROM hero_bracket_stats"),
        composer=None,
        max_rows=100,
        audience="consumer",
    )
    assert "Only the first 100 of 300 rows are shown" in res.response.answer
    assert "max_rows_to_return" not in res.response.answer and "Note:" not in res.response.answer
    assert res.response.truncated is not None and res.response.truncated.returned == 100


def test_the_composers_decline_reads_plain_for_the_consumer() -> None:
    llm = _Composer("NO_ANSWER: the data has no record of what a hero costs.")
    res = run_query(
        question="How much does Anti-Mage cost?",
        lens_name="dota",
        org_id="org-1",
        caller="visitor",
        semantic_model=_model(),
        connector=FakeConnector(result=_RESULT),
        generator=FixedSQLGenerator(_SQL),
        composer=AnswerComposer(llm),
        audience="consumer",
    )
    assert res.response.status == "refused"
    assert res.response.answer == (
        "I can't answer that from this data: the data has no record of what a hero costs."
    )
    assert "lens" not in res.response.answer


def test_a_date_as_a_person_writes_it() -> None:
    today = datetime.date(2026, 9, 27)
    assert spoken_date("2026-09-26", today=today) == "26 September"
    assert spoken_date("2026-09-26T03:00:00Z", today=today) == "26 September"
    assert spoken_date("2025-11-01", today=today) == "1 November 2025"
    assert spoken_date("not a date", today=today) == "not a date"
