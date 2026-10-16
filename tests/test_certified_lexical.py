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
from types import SimpleNamespace

from services.certify import store as certify_store
from services.certify.lexical import (
    answer_names,
    governed_names,
    lexical_mismatch,
    measure_names,
    names_in,
)
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
    assert names_in("how many active customers", _NAMES) == {"active_customer"}  # inflected
    assert names_in("invoiced_revenue vs collected_revenue", _NAMES) == {
        "invoiced_revenue",
        "collected_revenue",
    }


def test_regular_inflections_read_as_the_name() -> None:
    assert names_in("how much did we invoice in September?", _NAMES) == {"invoiced_revenue"}
    assert names_in("invoicing revenue by month", _NAMES) == {"invoiced_revenue"}
    # A word no other governed name shares is enough: "invoice" alone is about
    # invoiced revenue; "revenue" alone could be either.
    assert names_in("revenues invoiced in Q3", _NAMES) == {"invoiced_revenue"}
    assert names_in("how much revenue in Q3", _NAMES) == frozenset()
    short = frozenset({"mrr", "arr", "sales"})
    assert names_in("mrr today", short) == {"mrr"}
    assert names_in("mrrs today", short) == frozenset()  # short names match exactly


def test_answer_names_come_from_the_resolution_ledger() -> None:
    assert answer_names(None) == frozenset()
    ledger = {
        "slots": [
            {"kind": "metric", "name": "Collected_Revenue", "source": "declared"},
            {"kind": "metric", "name": "SUM(x)", "source": "inferred"},
            {"kind": "dimension", "name": "region", "source": "declared"},
        ]
    }
    assert answer_names(ledger) == {"collected_revenue"}


def test_mismatch_needs_a_governed_name_on_both_sides() -> None:
    assert lexical_mismatch("invoiced revenue in Q3?", "collected revenue in Q3?", _NAMES)
    assert not lexical_mismatch("revenue in Q3?", "collected revenue in Q3?", _NAMES)
    assert not lexical_mismatch("collected revenue, Q3", "Q3 collected revenue", _NAMES)
    assert not lexical_mismatch("a", "b", frozenset())


def test_governed_names_are_metrics_definitions_and_entities() -> None:
    names = governed_names(MODEL)
    assert {m.name for e in MODEL.entities for m in e.metrics} <= names
    assert {d.term for d in MODEL.definitions} <= names
    assert {e.name for e in MODEL.entities} <= names
    assert measure_names(MODEL) == names - {e.name for e in MODEL.entities}


# ── the serve veto ───────────────────────────────────────────────────────────


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = "What was collected revenue in Q3 2026?"
    sql: str = "SELECT SUM(collected) FROM ledger WHERE q = 3"
    slots: dict[str, str] | None = None
    sample_bindings: list[dict[str, str]] | None = None
    source: str | None = None
    resolution: dict[str, object] | None = None


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


def test_a_template_hit_is_vetoed_before_binding(monkeypatch) -> None:
    # "how much did we invoice in September" must not bind the collections
    # template, however well its slot fills.
    template = _Answer(
        question="What was collected revenue in {month}?",
        sql="SELECT SUM(collected) FROM ledger WHERE month = '{month}'",
        slots={"month": "text"},
        sample_bindings=[{"month": "2026-09"}],
    )
    _wire(monkeypatch, [_Hit(template)])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(assembly, "certified_bind", lambda *_a, **_k: {"month": "2026-09"})
    top, exemplars, mode, match, bound = certified_lookup(
        "l", [0.1], "org", "how much did we invoice in September?", governed=_NAMES
    )
    assert top is None and match is None and bound is None and exemplars == []
    top, _ex, mode, match, bound = certified_lookup(
        "l", [0.1], "org", "what did we collect in September?", governed=_NAMES
    )
    assert top is not None and match == "parameterized" and bound == {"month": "2026-09"}


def test_the_answers_own_ledger_names_count(monkeypatch) -> None:
    # The approved wording names nothing governed; its resolution ledger does.
    stored = _Answer(
        question="What came in during Q3 2026?",
        resolution={
            "slots": [{"kind": "metric", "name": "collected_revenue", "source": "declared"}]
        },
    )
    _wire(monkeypatch, [_Hit(stored)])
    top, _ex, mode, match, _bound = certified_lookup(
        "l", [0.1], "org", "What was invoiced revenue in Q3 2026?", governed=_NAMES
    )
    assert top is None and mode == "assisted"


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


def test_a_review_answer_the_file_does_not_carry_is_checked_too(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    stored = _Hit(
        _Answer(question="collected revenue last quarter?", sql=_SQL_B, source="review:r1"), 0.97
    )
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [stored])
    source = _source([{"question": "invoiced revenue last quarter?", "sql": _SQL_A}])
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1 and "collected revenue last quarter?" in errors[0]


def test_a_rename_is_not_a_pair_with_the_row_it_replaces(monkeypatch) -> None:
    # The file carries the renamed question with new SQL; the stored row under
    # the old wording is file-originated, so this apply deletes it — comparing
    # against it would demand a two-step apply for every rename.
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    stored = _Hit(_Answer(question="revenue last quarter?", sql=_SQL_B), 0.97)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [stored])
    source = _source([{"question": "total revenue in the last quarter?", "sql": _SQL_A}])
    assert apply_mod._cross_match_errors(None, "customer_value", source, MODEL) == []  # type: ignore[arg-type]


def test_different_governed_names_above_the_band_warn_instead_of_failing(monkeypatch) -> None:
    # "MRR today" vs "ARR today": close by cosine, told apart at serve by the
    # lexical veto — a warning, never a rejected plan.
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [])
    source = _source(
        [
            {"question": "average order value last quarter revenue?", "sql": _SQL_A},
            {"question": "lifetime value last quarter revenue?", "sql": _SQL_B},
        ]
    )
    warnings: list[str] = []
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL, warnings)  # type: ignore[arg-type]
    assert errors == []
    assert len(warnings) == 1 and "different governed terms" in warnings[0]
    assert "average_order_value" in warnings[0] and "lifetime_value" in warnings[0]


def test_a_shared_entity_name_alone_fails_only_in_the_exact_band(monkeypatch) -> None:
    class _Near:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0], [0.92, 0.3919]]  # cosine 0.92: above equiv, below exact

    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [])
    entries = [
        {"question": "how many customers churned?", "sql": "SELECT count(*) FROM customers"},
        {"question": "which customers churned?", "sql": "SELECT first_name FROM customers"},
    ]
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Near())
    warnings: list[str] = []
    errors = apply_mod._cross_match_errors(  # type: ignore[arg-type]
        None, "customer_value", _source(entries), MODEL, warnings
    )
    assert errors == [] and len(warnings) == 1 and "only the score" in warnings[0]
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())  # identical vectors: 1.0
    errors = apply_mod._cross_match_errors(None, "customer_value", _source(entries), MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1


def test_no_embedder_measures_nothing(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "_embedder", lambda: None)
    source = _source(
        [
            {"question": "invoiced revenue last quarter?", "sql": _SQL_A},
            {"question": "collected revenue last quarter?", "sql": _SQL_B},
        ]
    )
    assert apply_mod._cross_match_errors(None, "customer_value", source, MODEL) == []  # type: ignore[arg-type]
