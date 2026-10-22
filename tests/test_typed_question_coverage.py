"""Every word that qualifies what is counted must reach the answer.

"How many deleted members joined in May?" on a lens that holds no deleted
flag served every member who joined: the qualifier mapped to nothing, the
filter vote rightly found no column for it, and the plain count went out as
the answer. A content word standing beside the counted noun or the metric's
own words must map to the chosen metric, a dimension or field, a filter or
binding, a definition, or the period; one that maps to nothing clarifies.
A word that stands alone (a synonym of the figure, "earnings") is the metric
vote's, as before, and a word only an unchosen metric carries asks which of
those metrics is meant.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.typed_resolver import TypedResolver

TODAY = date(2026, 10, 22)


class _Chooser:
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


def _gym(*fields: Field, definitions: list[Definition] | None = None) -> SemanticModel:
    """A climbing gym's members, one row per member."""
    members = Entity(
        name="members",
        source=EntitySource(connection="wh", table="members"),
        default_time_field="joined_on",
        primary_key=["member_id"],
        grain="one row per member",
        fields=[
            Field(name="member_id", type="string"),
            Field(name="joined_on", type="date"),
            Field(name="plan", type="string"),
            Field(name="visits", type="integer"),
            *fields,
        ],
        dimensions=[Dimension(name="plan")],
        metrics=[
            Metric(name="member_count", agg="count", expr="members.member_id", label="members"),
            Metric(name="total_visits", agg="sum", expr="members.visits"),
            Metric(
                name="member_change",
                agg="sum",
                expr="members.visits",
                label="net member change",
            ),
        ],
    )
    return SemanticModel(
        lens="gym", dialect="duckdb", entities=[members], definitions=definitions or []
    )


def _resolve(question: str, model: SemanticModel | None = None, *prefer: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Chooser(*(prefer or ("member_count",))),  # type: ignore[arg-type]
        domains={"plan": ["basic", "premium"]},
        today=TODAY,
    ).resolve(question, model or _gym())


def test_a_qualifier_the_lens_holds_nowhere_clarifies() -> None:
    res = _resolve("How many deleted members joined in May 2026?")
    assert res.intent is None
    assert res.clarification is not None and res.clarification.term == "deleted"


def test_a_qualifier_on_a_figure_clarifies() -> None:
    res = _resolve("What were the total visits of suspended members in 2026?", None, "total_visits")
    assert res.clarification is not None and res.clarification.term == "suspended"


def test_a_stored_value_qualifier_filters() -> None:
    res = _resolve("How many premium members joined in May 2026?", None, "member_count", "plan")
    assert res.intent is not None, res.clarification
    assert ("plan", "premium") in [(f.field, f.value) for f in res.intent.filters]


def test_the_plain_question_acts() -> None:
    res = _resolve("How many members joined in May 2026?")
    assert res.intent is not None, res.clarification


def test_a_word_standing_alone_is_the_votes() -> None:
    res = _resolve("What was the footfall in May 2026?", None, "total_visits")
    assert res.intent is not None, res.clarification


def test_a_definition_the_question_names_maps_its_word() -> None:
    lapsed = Definition(
        term="lapsed",
        body="a member with no visit in 90 days",
        sql_expr="members.visits = 0",
        about="members",
    )
    res = _resolve("How many lapsed members joined in 2026?", _gym(definitions=[lapsed]), "engaged")
    assert res.intent is not None, res.clarification
    assert res.intent.definitions == ["lapsed"]


def test_a_field_the_question_names_is_mapped() -> None:
    res = _resolve(
        "How many members by home gym joined in May 2026?",
        _gym(Field(name="home_gym", type="string")),
    )
    assert res.clarification is None or res.clarification.term != "home"


def test_a_word_only_another_metric_carries_asks_which_metric() -> None:
    res = _resolve("How many net members in May 2026?")
    assert res.clarification is not None
    assert res.clarification.term == "net"
    assert "member_change" in (res.clarification.options or [])


def test_new_beside_a_window_is_the_window() -> None:
    res = _resolve("How many new members in May 2026?")
    assert res.intent is not None, res.clarification


def test_new_with_no_window_clarifies() -> None:
    res = _resolve("How many new members are there?")
    assert res.clarification is not None and res.clarification.term == "new"


def test_a_qualifier_beside_a_named_flag_clarifies() -> None:
    # The flag is named by its own word ("student"): its filter is the words',
    # so a further word that maps to nothing ("suspended") is still asked.
    res = _resolve(
        "How many suspended student members joined in May 2026?",
        _gym(Field(name="is_student", type="boolean")),
        "member_count",
        "true",
    )
    assert res.intent is None
    assert res.clarification is not None and res.clarification.term == "suspended"


def test_a_named_flag_alone_still_filters() -> None:
    res = _resolve(
        "How many student members joined in May 2026?",
        _gym(Field(name="is_student", type="boolean")),
        "member_count",
        "true",
    )
    assert res.intent is not None, res.clarification
    assert ("is_student", "true") in [(f.field, str(f.value)) for f in res.intent.filters]


def test_a_flag_named_in_its_hyphenated_form_maps_its_words() -> None:
    # "off-peak" names `is_offpeak` read joined; neither "off" nor "peak" is
    # a word of the column's name, and neither is a qualifier.
    res = _resolve(
        "How many off-peak members joined in May 2026?",
        _gym(Field(name="is_offpeak", type="boolean")),
        "member_count",
        "true",
    )
    assert res.intent is not None, res.clarification
    assert ("is_offpeak", "true") in [(f.field, str(f.value)) for f in res.intent.filters]
