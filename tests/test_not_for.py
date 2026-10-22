"""A lens's declared `not_for` asks refuse before typing or generation.

Each ask is a figure the lens's tables could produce and should not: the
number would be well-formed and about a different measure. The match is the
ask's content words, all of them, word for word; a question sharing one word
with an ask is not that ask.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.not_for import content_words, declared_asks, not_for_refusal
from services.runtime.pipeline import run_query

# (lens, entity, ask, sibling, a question naming the ask)
ASKS = [
    (
        "ticket_sales",
        "ticket_sales",
        "door-scan attendance",
        "attendance",
        "Show me door-scan attendance against capacity",
    ),
    (
        "seat_refunds",
        "seat_refunds",
        "booking-level refunds",
        "booking_refunds",
        "booking-level refunds, where the whole booking is refunded",
    ),
    (
        "box_office",
        "box_office",
        "performance-date takings",
        "performance_takings",
        "performance-date takings",
    ),
    (
        "seat_inventory",
        "seat_inventory",
        "patron or seat counts",
        "patron_counts",
        "patron or seat counts",
    ),
]


def _model(lens: str, entity: str, not_for: list[dict[str, str]], use_cases=()) -> SemanticModel:
    return SemanticModel(
        lens=lens,
        dialect="duckdb",
        entities=[
            Entity(
                name=entity,
                source=EntitySource(connection="wh", table=entity),
                use_cases=list(use_cases),
                fields=[Field(name="amount", type="number")],
                metrics=[Metric(name="total", agg="sum", expr=f"{entity}.amount")],
            )
        ],
        not_for=not_for,
    )


@pytest.mark.parametrize(("lens", "entity", "ask", "sibling", "question"), ASKS)
def test_each_declared_ask_refuses_naming_the_sibling(
    lens: str, entity: str, ask: str, sibling: str, question: str
) -> None:
    model = _model(lens, entity, [{"ask": ask, "sibling": sibling}])
    assert not_for_refusal(question, model) == f"this lens does not answer {ask}; ask {sibling}"


@pytest.mark.parametrize(
    ("ask", "question"),
    [
        ("door-scan attendance", "What was ticketed attendance in September?"),
        ("booking-level refunds", "What were seat refunds in September?"),
        ("performance-date takings", "What were takings last month?"),
        ("patron or seat counts", "How many seats were released in September?"),
    ],
)
def test_a_question_sharing_one_word_is_not_refused(ask: str, question: str) -> None:
    assert not_for_refusal(question, _model("l", "t", [{"ask": ask, "sibling": "x"}])) is None


def test_words_match_whole_and_ignore_function_words() -> None:
    assert content_words("patron or seat counts") == ["patron", "seat", "counts"]
    model = _model("l", "t", [{"ask": "door-scan attendance", "sibling": ""}])
    assert not_for_refusal("doorscan attendances", model) is None
    assert (
        not_for_refusal("DOOR_SCAN attendance by month", model)
        == "this lens does not answer door-scan attendance"
    )


def test_avoid_for_use_cases_are_asks_with_no_sibling() -> None:
    model = _model(
        "seat_refunds",
        "seat_refunds",
        [],
        use_cases=[
            "Use when: seat-level refunds and exchanges",
            "Avoid for: booking-level refunds (see the bookings lens); performance-date takings",
        ],
    )
    assert [a.ask for a in declared_asks(model)] == [
        "booking-level refunds",
        "performance-date takings",
    ]
    assert (
        not_for_refusal("booking level refunds in Q3", model)
        == "this lens does not answer booking-level refunds"
    )
    assert not_for_refusal("seat refunds in Q3", model) is None


def test_avoid_for_lines_are_ignored_on_a_lens_with_several_entities() -> None:
    model = _model("l", "a", [], use_cases=["Avoid for: booking-level refunds"])
    other = Entity(
        name="bookings",
        source=EntitySource(connection="wh", table="bookings"),
        fields=[Field(name="amount", type="number")],
    )
    model = model.model_copy(update={"entities": [*model.entities, other]})
    assert declared_asks(model) == []


def test_lens_yaml_carries_not_for() -> None:
    cfg = LensConfig.model_validate(
        {"name": "ticket_sales", "not_for": [{"ask": "door-scan attendance", "sibling": "scans"}]}
    )
    assert cfg.not_for[0].ask == "door-scan attendance" and cfg.not_for[0].sibling == "scans"


def test_the_pipeline_refuses_before_any_generation(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE ticket_sales (amount DOUBLE)")
    con.execute("INSERT INTO ticket_sales VALUES (1.0)")
    con.close()
    res = run_query(
        question="Show me door-scan attendance against capacity",
        lens_name="ticket_sales",
        org_id="org",
        caller="t",
        semantic_model=_model(
            "ticket_sales",
            "ticket_sales",
            [{"ask": "door-scan attendance", "sibling": "attendance"}],
        ),
        connector=DuckDBConnector(str(tmp_path / "wh.duckdb")),
        generator=GroundedSQLGenerator(ScriptedLLM(["SHOULD NEVER BE CALLED"])),
        composer=None,
    )
    assert res.trace.status == "refused"
    assert res.response.answer is not None
    assert "does not answer door-scan attendance; ask attendance" in res.response.answer


@pytest.mark.parametrize(
    ("certified_question", "refused"),
    [
        # Matched to another question: nobody approved that SQL for this ask.
        ("How much did we take per show?", True),
        # The certified question IS the asked one: a human approved exactly it.
        ("Show me door-scan attendance against capacity", False),
    ],
)
def test_a_certified_match_to_another_question_is_no_exemption(
    tmp_path: Path, certified_question: str, refused: bool
) -> None:
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE ticket_sales (amount DOUBLE)")
    con.execute("INSERT INTO ticket_sales VALUES (1.0)")
    con.close()
    res = run_query(
        question="Show me door-scan attendance against capacity",
        lens_name="ticket_sales",
        org_id="org",
        caller="t",
        semantic_model=_model(
            "ticket_sales",
            "ticket_sales",
            [{"ask": "door-scan attendance", "sibling": "attendance"}],
        ),
        connector=DuckDBConnector(str(tmp_path / "wh.duckdb")),
        generator=FixedSQLGenerator("SELECT SUM(amount) AS total FROM ticket_sales"),
        composer=None,
        certification="certified",
        certified_match="equivalent",
        certified_question=certified_question,
    )
    assert (res.trace.status == "refused") is refused
