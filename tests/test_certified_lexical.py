"""Two governed names are not a paraphrase of each other.

Cosine similarity put an invoicing question inside the exact band of a
collections template, and the collections SQL served as ``certified``. Two
rails pin the class: at serve, a deterministic lexical veto drops a plain hit
whose approved question names a different governed metric or definition than
the asked one; at plan and apply, a dry run of every certified question against
the corpus fails the plan when two answers with different SQL would answer
each other's question. Pure: search, session and embedder are stand-ins."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from services.certify import store as certify_store
from services.certify.lexical import governed_names, lexical_mismatch, names_in
from services.lenses.demo import jaffle_customer_value_bundle
from services.project import apply as apply_mod
from services.project.loader import LensSource
from services.runtime import assembly
from services.runtime.assembly import certified_lookup

MODEL = jaffle_customer_value_bundle().semantic_model
_NAMES = frozenset({"invoiced_revenue", "collected_revenue", "active_customer"})


def test_names_in_reads_whole_words_with_underscores_as_spaces() -> None:
    assert names_in("What was invoiced revenue last quarter?", _NAMES) == {"invoiced_revenue"}
    assert names_in("collected-revenue by month", _NAMES) == {"collected_revenue"}
    assert names_in("how many active customers", _NAMES) == frozenset()  # plural is not the name
    assert names_in("invoiced_revenue vs collected_revenue", _NAMES) == {
        "invoiced_revenue",
        "collected_revenue",
    }


def test_mismatch_needs_a_governed_name_on_both_sides() -> None:
    assert lexical_mismatch("invoiced revenue in Q3?", "collected revenue in Q3?", _NAMES)
    assert not lexical_mismatch("revenue in Q3?", "collected revenue in Q3?", _NAMES)
    assert not lexical_mismatch("collected revenue, Q3", "Q3 collected revenue", _NAMES)
    assert not lexical_mismatch("a", "b", frozenset())


def test_governed_names_are_metrics_and_definitions() -> None:
    names = governed_names(MODEL)
    assert {m.name for e in MODEL.entities for m in e.metrics} <= names
    assert {d.term for d in MODEL.definitions} <= names


# ── the serve veto ───────────────────────────────────────────────────────────


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = "What was collected revenue in Q3 2026?"
    sql: str = "SELECT SUM(collected) FROM ledger WHERE q = 3"
    slots: dict[str, str] | None = None


@dataclass
class _Hit:
    answer: _Answer = field(default_factory=_Answer)
    score: float = 0.995


def _wire(monkeypatch, hits: list[_Hit]) -> None:
    @contextlib.contextmanager
    def _null_session(_org):  # noqa: ANN001
        yield None

    monkeypatch.setattr(assembly, "org_session", _null_session)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: hits)
    monkeypatch.setattr(assembly, "assist_llm", lambda: None)


def test_a_different_governed_name_drops_an_exact_hit_to_assisted(monkeypatch) -> None:
    _wire(monkeypatch, [_Hit()])
    top, exemplars, mode, match, _bound = certified_lookup(
        "l", [0.1], "org", "What was invoiced revenue in Q3 2026?", governed=_NAMES
    )
    assert top is None and match is None and mode == "assisted"
    assert [e.sql for e in exemplars] == [_Answer().sql]


def test_the_same_governed_name_still_serves_exact(monkeypatch) -> None:
    _wire(monkeypatch, [_Hit()])
    top, _ex, mode, match, _bound = certified_lookup(
        "l", [0.1], "org", "Collected revenue, Q3 2026?", governed=_NAMES
    )
    assert top is not None and mode == "certified" and match == "exact"


def test_without_governed_names_the_score_decides(monkeypatch) -> None:
    _wire(monkeypatch, [_Hit()])
    top, _ex, mode, match, _bound = certified_lookup(
        "l", [0.1], "org", "What was invoiced revenue in Q3 2026?"
    )
    assert top is not None and match == "exact"


# ── the plan-time dry run ────────────────────────────────────────────────────


class _Embedder:
    """Questions that share a word family embed close; the rest far apart."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "revenue" in t else [0.0, 1.0] for t in texts]


def _source(entries: list[dict[str, object]]) -> LensSource:
    bundle = jaffle_customer_value_bundle()
    return LensSource(
        config=bundle.config,
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        certified_answers=entries,
        eval_cases=[],
    )


_SQL_A = "SELECT SUM(invoiced) FROM orders"
_SQL_B = "SELECT SUM(collected) FROM orders"


def test_two_incoming_answers_in_the_paraphrase_band_fail_the_plan(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [])
    source = _source(
        [
            {"question": "invoiced revenue last quarter?", "sql": _SQL_A},
            {"question": "collected revenue last quarter?", "sql": _SQL_B},
            {"question": "how many customers?", "sql": "SELECT count(*) FROM customers"},
        ]
    )
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1
    assert "would answer each other's question" in errors[0]
    assert "invoiced revenue last quarter?" in errors[0]
    assert "collected revenue last quarter?" in errors[0]


def test_same_sql_under_two_phrasings_is_one_answer(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [])
    source = _source(
        [
            {"question": "invoiced revenue last quarter?", "sql": _SQL_A},
            {"question": "revenue invoiced in the last quarter?", "sql": _SQL_A.upper()},
        ]
    )
    assert apply_mod._cross_match_errors(None, "customer_value", source, MODEL) == []  # type: ignore[arg-type]


def test_a_stored_answer_the_file_does_not_carry_is_checked_too(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    stored = _Hit(_Answer(question="collected revenue last quarter?", sql=_SQL_B), 0.97)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [stored])
    source = _source([{"question": "invoiced revenue last quarter?", "sql": _SQL_A}])
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1 and "collected revenue last quarter?" in errors[0]


def test_no_embedder_measures_nothing(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: None)
    source = _source(
        [
            {"question": "invoiced revenue last quarter?", "sql": _SQL_A},
            {"question": "collected revenue last quarter?", "sql": _SQL_B},
        ]
    )
    assert apply_mod._cross_match_errors(None, "customer_value", source, MODEL) == []  # type: ignore[arg-type]
