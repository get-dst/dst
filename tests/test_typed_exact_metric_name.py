"""A metric the question names exactly outranks one whose name it only overlaps.

"Net new members" is the alias of the net metric. The count of new members is
named `new_members`, so its whole name sits inside the asked phrase, and a vote
that settles on it serves the joiners alone as the net change. A label or
alias said word for word is a declared fact: the metric it names is the
answer's, unless the question also says words of the voted metric outside
that phrase.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.typed_resolver import TypedResolver


class _Chooser:
    """Picks the first preferred name among the options offered, else `none`
    when it is offered, else the first option."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = ("aggregate", *prefer)

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        chosen = next(
            (p for p in self.prefer if p in names), "none" if "none" in names else names[0]
        )
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _gym() -> SemanticModel:
    """A climbing gym's monthly membership movements."""
    movements = Entity(
        name="membership_moves",
        source=EntitySource(connection="wh", table="membership_moves"),
        default_time_field="moved_on",
        primary_key=["move_id"],
        grain="one row per membership change",
        fields=[
            Field(name="move_id", type="string"),
            Field(name="moved_on", type="date"),
            Field(name="joined", type="integer"),
            Field(name="left_gym", type="integer"),
        ],
        metrics=[
            Metric(name="new_members", agg="sum", expr="membership_moves.joined"),
            Metric(name="leavers", agg="sum", expr="membership_moves.left_gym"),
            Metric(
                name="member_change",
                type="derived",
                expr="{new_members} - {leavers}",
                label="net member change",
                aliases=["net new members"],
            ),
        ],
    )
    return SemanticModel(lens="gym", dialect="duckdb", entities=[movements])


def _resolve(question: str, *prefer: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Chooser(*prefer),  # type: ignore[arg-type]
        today=date(2026, 10, 22),
    ).resolve(question, _gym())


def test_an_alias_said_word_for_word_outranks_a_metric_inside_it() -> None:
    res = _resolve("What were net new members in May 2026?", "new_members")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["member_change"]


def test_a_label_said_word_for_word_outranks_the_vote() -> None:
    res = _resolve("Show the net member change for May 2026", "new_members")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["member_change"]


def test_the_vote_stands_where_the_question_says_its_words_elsewhere() -> None:
    # "leavers" is said outside the exact phrase: the vote's pick is read
    # from the question too, so the text does not overrule it.
    res = _resolve("How many leavers against the net member change in May 2026?", "leavers")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics[0] == "leavers"


def test_a_plain_name_the_vote_picked_is_kept() -> None:
    res = _resolve("How many new members joined in May 2026?", "new_members")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["new_members"]
