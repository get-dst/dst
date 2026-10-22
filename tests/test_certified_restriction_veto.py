"""A certified answer serves only the restriction its own question states.

"How many guided tours were there in May 2026?" embeds within the exact band of
the certified "How many tours were there in May 2026?", and the approved count
of every tour was served for the guided ones. A stored value the question names
("guided", a value of `tour_kind`) or a true/false field it names by its subject
("accessible tours" of `is_accessible`) that the approved question does not
state, and its SQL does not filter on, drops the hit to the typed path.
Pure: search and org_session are monkeypatched, as in the other veto tests."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from services.certify import store as certify_store
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime import assembly
from services.runtime.assembly import certified_lookup, named_restrictions

_Q = "How many tours were there in May 2026?"
_SQL = (
    "SELECT COUNT(*) AS tours FROM tours "
    "WHERE tour_day >= DATE '2026-05-01' AND tour_day < DATE '2026-06-01'"
)


def _model() -> SemanticModel:
    tours = Entity(
        name="tours",
        source=EntitySource(connection="wh", table="tours"),
        default_time_field="tour_day",
        primary_key=["tour_id"],
        fields=[
            Field(name="tour_id", type="string"),
            Field(name="tour_day", type="date"),
            Field(name="tour_kind", type="string"),
            Field(name="is_accessible", type="boolean"),
        ],
        metrics=[Metric(name="tours", agg="count")],
    )
    return SemanticModel(lens="tours", dialect="duckdb", entities=[tours])


_SAID = named_restrictions(_model(), {"tours": {"tour_kind": ["Guided", "Self-guided", "2026"]}})


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = _Q
    sql: str = _SQL
    slots: dict[str, str] | None = None
    resolution: dict[str, object] | None = None
    terms: list[str] | None = None


@dataclass
class _Hit:
    answer: _Answer = field(default_factory=_Answer)
    score: float = 0.95


def _wire(monkeypatch, hits: list[_Hit]) -> None:  # noqa: ANN001
    @contextlib.contextmanager
    def _null_session(_org):  # noqa: ANN001
        yield None

    monkeypatch.setattr(assembly, "org_session", _null_session)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: hits)


def _lookup(question: str):  # type: ignore[no-untyped-def]
    return certified_lookup("tours", [0.1], "org-1", question, restrictions=_SAID)


def test_a_stored_value_the_approved_question_lacks_does_not_serve(monkeypatch) -> None:  # noqa: ANN001
    _wire(monkeypatch, [_Hit()])
    top, exemplars, mode, match, _ = _lookup("How many guided tours were there in May 2026?")
    assert top is None and match is None and mode == "assisted"
    assert exemplars and exemplars[0].sql == _SQL


def test_a_named_flag_the_approved_question_lacks_does_not_serve(monkeypatch) -> None:  # noqa: ANN001
    _wire(monkeypatch, [_Hit()])
    top, _, _mode, _match, _ = _lookup("How many accessible tours were there in May 2026?")
    assert top is None


def test_the_flags_other_side_does_not_serve_either(monkeypatch) -> None:  # noqa: ANN001
    accessible = _Answer(question="How many accessible tours were there in May 2026?")
    _wire(monkeypatch, [_Hit(answer=accessible)])
    top, _, _mode, _match, _ = _lookup("How many non-accessible tours were there in May 2026?")
    assert top is None


def test_the_approved_question_itself_still_serves(monkeypatch) -> None:  # noqa: ANN001
    _wire(monkeypatch, [_Hit()])
    top, _, mode, match, _ = _lookup(_Q)
    assert top is not None and mode == "certified" and match == "exact"


def test_a_value_the_approved_sql_filters_on_still_serves(monkeypatch) -> None:  # noqa: ANN001
    guided = _Answer(
        question="How many guided tours in May 2026?",
        sql=_SQL + " AND tour_kind = 'Guided'",
    )
    _wire(monkeypatch, [_Hit(answer=guided)])
    top, _, _mode, match, _ = _lookup("How many guided tours were there in May 2026?")
    assert top is not None and match == "exact"


def test_numbers_are_not_restrictions() -> None:
    # A stored "2026" is not a restriction a period's year names.
    assert ("tour_kind", "2026") not in _SAID("How many tours in May 2026?")


def test_a_restriction_only_the_approved_question_states_does_not_serve(monkeypatch) -> None:  # noqa: ANN001
    guided = _Answer(
        question="How many guided tours were there in May 2026?",
        sql=_SQL + " AND tour_kind = 'Guided'",
    )
    _wire(monkeypatch, [_Hit(answer=guided)])
    top, _, _mode, _match, _ = _lookup(_Q)
    assert top is None
