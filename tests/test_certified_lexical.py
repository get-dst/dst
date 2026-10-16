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
from services.contracts.semantic_model import Definition, Dimension, Metric
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
    terms: list[str] | None = None


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


def test_a_raw_template_never_serves_a_question_about_a_governed_term(monkeypatch) -> None:
    """The asked question says "invoice", a definition term; the candidate's
    question and SQL name nothing governed. Word by word, the candidate does
    not say the asked question's governed word, so the exact hit drops to
    assisted — no model, no score, decides it."""
    model = MODEL.model_copy(deep=True)
    model.definitions.append(Definition(term="invoiced", body="billed, paid or not"))
    collections = _Answer(
        question="how much was collected in {month}?",
        sql="SELECT SUM(amount) FROM orders WHERE paid_at >= '{month.start}'",
        slots={"month": {"type": "date_range"}},
        sample_bindings=[{"month": "2026-09"}],
        terms=["amount"],
    )
    _wire(monkeypatch, [_Hit(collections)])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(assembly, "certified_bind", lambda *_a, **_k: {"month": "2026-09"})
    answer, _ex, mode, match, _bound = certified_lookup(
        "customer_value",
        [1.0],
        "org",
        "how much did we invoice in September 2026?",
        governed=governed_names(model),
    )
    assert answer is None and mode != "certified" and match is None
    # The same candidate SAYING the word (its question or the column it sums) serves.
    invoiced = _Answer(
        question="how much was invoiced in {month}?",
        sql=collections.sql,
        slots=collections.slots,
        sample_bindings=collections.sample_bindings,
        terms=["amount"],
    )
    _wire(monkeypatch, [_Hit(invoiced)])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(assembly, "certified_bind", lambda *_a, **_k: {"month": "2026-09"})
    answer, _ex, mode, match, _bound = certified_lookup(
        "customer_value",
        [1.0],
        "org",
        "how much did we invoice in September 2026?",
        governed=governed_names(model),
    )
    assert answer is invoiced and mode == "certified" and match == "parameterized"
    by_column = _Answer(
        question="how much came in during {month}?",
        sql="SELECT SUM(invoiced_amount) FROM orders WHERE paid_at >= '{month.start}'",
        slots=collections.slots,
        sample_bindings=collections.sample_bindings,
        terms=["invoiced_amount"],
    )
    _wire(monkeypatch, [_Hit(by_column)])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(assembly, "certified_bind", lambda *_a, **_k: {"month": "2026-09"})
    answer, _ex, mode, _m, _bound = certified_lookup(
        "customer_value",
        [1.0],
        "org",
        "how much did we invoice in September 2026?",
        governed=governed_names(model),
    )
    assert answer is by_column and mode == "certified"


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
            {"question": "what was the revenue invoiced in the last quarter?", "sql": _SQL_B},
            {"question": "how many customers?", "sql": "SELECT count(*) FROM customers"},
        ]
    )
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1
    assert "would answer each other's question" in errors[0]
    assert "invoiced revenue last quarter?" in errors[0]
    assert "what was the revenue invoiced in the last quarter?" in errors[0]


def test_a_word_one_question_says_and_the_other_does_not_keeps_them_apart(monkeypatch) -> None:
    """Two questions over one metric that differ in a grain word, a shape
    word or a dimension are two questions: a warning, never a rejected plan."""
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [])
    model = MODEL.model_copy(deep=True)
    orders = next(e for e in model.entities if e.name == "orders")
    orders.metrics.append(Metric(name="mrr", agg="sum", expr="orders.amount"))
    orders.metrics.append(Metric(name="otd_rate", agg="avg", expr="orders.amount"))
    orders.dimensions.append(Dimension(name="service_level", expr="orders.status"))
    pairs = [
        ("what was MRR revenue at the end of September?", "by how much did MRR revenue grow?"),
        ("MRR revenue level today?", "MRR revenue growth today?"),
        ("otd_rate revenue by month?", "otd_rate revenue by service level?"),
        ("invoiced revenue last quarter?", "collected revenue last quarter?"),
    ]
    for one, other in pairs:
        warnings: list[str] = []
        source = _source([{"question": one, "sql": _SQL_A}, {"question": other, "sql": _SQL_B}])
        errors = apply_mod._cross_match_errors(  # type: ignore[arg-type]
            None, "customer_value", source, model, warnings
        )
        assert errors == [], (one, other, errors)
        assert len(warnings) == 1 and "keeps them apart" in warnings[0], (one, other)


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
        _Answer(question="last quarter's invoiced revenue?", sql=_SQL_B, source="review:r1"), 0.97
    )
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: [stored])
    source = _source([{"question": "invoiced revenue last quarter?", "sql": _SQL_A}])
    errors = apply_mod._cross_match_errors(None, "customer_value", source, MODEL)  # type: ignore[arg-type]
    assert len(errors) == 1 and "last quarter's invoiced revenue?" in errors[0]


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
    assert errors == [] and len(warnings) == 1 and "which" in warnings[0]
    monkeypatch.setattr(apply_mod, "_embedder", lambda: _Embedder())  # identical vectors: 1.0
    # "which" is a word one question says and the other does not: a listing
    # against a count stays a warning even at 1.0.
    warnings = []
    errors = apply_mod._cross_match_errors(  # type: ignore[arg-type]
        None, "customer_value", _source(entries), MODEL, warnings
    )
    assert errors == [] and len(warnings) == 1 and "which" in warnings[0]
    same_words = [
        {"question": "how many customers churned?", "sql": "SELECT count(*) FROM customers"},
        {"question": "number of churned customers?", "sql": "SELECT count(1) FROM customers"},
    ]
    errors = apply_mod._cross_match_errors(None, "customer_value", _source(same_words), MODEL)  # type: ignore[arg-type]
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


def _invoices_model():
    """Two metrics over one column that differ by filter alone, beside a count
    that ties with another entity's (as a real finance layer has them)."""
    from services.contracts import semantic_model as sm

    invoices = sm.Entity(
        name="invoices",
        source={"connection": "warehouse", "table": "marts.fct_invoices"},
        default_time_field="invoice_date",
        fields=[
            sm.Field(name="invoice_date", type="date"),
            sm.Field(name="paid_at", type="date"),
            sm.Field(name="amount_eur", type="number"),
            sm.Field(name="status", type="string"),
        ],
        metrics=[
            sm.Metric(
                name="collected_eur",
                agg="sum",
                expr="invoices.amount_eur",
                filters=["invoices.status = 'paid'"],
                agg_time_field="paid_at",
            ),
            sm.Metric(name="invoiced_eur", agg="sum", expr="invoices.amount_eur"),
            sm.Metric(name="invoice_count", agg="count"),
        ],
    )
    customers = sm.Entity(
        name="customers",
        source={"connection": "warehouse", "table": "marts.dim_customers"},
        fields=[sm.Field(name="customer_id", type="string")],
        metrics=[sm.Metric(name="customer_count", agg="count")],
    )
    return sm.SemanticModel(
        lens="finance",
        dialect="duckdb",
        entities=[invoices, customers],
        definitions=[
            Definition(
                term="collected_revenue",
                body="cash basis",
                sql_expr="SUM(invoices.amount_eur) FILTER (WHERE invoices.status = 'paid')",
            )
        ],
    )


_COLLECTIONS_SQL = (
    "select round(sum(amount_eur), 2) as collected_eur, count(*) as paid_invoices "
    "from marts.fct_invoices where status = 'paid' "
    "and paid_at >= date '2026-01-01' and paid_at < date '2026-07-01'"
)


def test_terms_name_the_metric_a_raw_aggregate_is() -> None:
    """``SUM(amount_eur)`` under ``WHERE status = 'paid'`` is collected_eur, not
    the unfiltered invoiced_eur the column alone would equally name; the same
    sum without the filter is invoiced_eur."""
    from services.runtime.resolution import governed_terms

    model = _invoices_model()
    assert governed_terms(_COLLECTIONS_SQL, model) == ["amount_eur", "collected_eur"]
    unfiltered = (
        "select round(sum(amount_eur), 2) as invoiced_eur from marts.fct_invoices "
        "where invoice_date >= date '2026-09-01' and invoice_date < date '2026-10-01'"
    )
    assert governed_terms(unfiltered, model) == ["amount_eur", "invoiced_eur"]


def test_the_measure_veto_keeps_an_invoicing_ask_off_a_collections_template() -> None:
    """ "How much did we invoice in September 2026?" says invoiced_eur or
    invoice_count by its words; a template attributed to collected_eur computes
    neither — vetoed. The template's own wording, and a cash question, still
    take it: the asked words reach collected_eur too."""
    model = _invoices_model()
    names, measures = governed_names(model), measure_names(model)
    approved = "how much invoiced revenue did we collect in {period}"
    terms = frozenset({"amount_eur", "collected_eur"})
    assert lexical_mismatch(
        "How much did we invoice in September 2026?", approved, names, frozenset(), terms, measures
    )
    assert lexical_mismatch(
        "how many invoices did we issue in Q3 2026", approved, names, frozenset(), terms, measures
    )
    assert not lexical_mismatch(
        "how much invoiced revenue did we collect in Q3 2026",
        approved,
        names,
        frozenset(),
        terms,
        measures,
    )
    assert not lexical_mismatch(
        "how much cash did we collect in September", approved, names, frozenset(), terms, measures
    )
    # Without the measure set (or without terms) the veto has nothing to read:
    # the asked and approved questions both say "invoice", and agree.
    assert not lexical_mismatch(
        "How much did we invoice in September 2026?", approved, names, frozenset(), terms
    )


# ── labels, aliases and units ────────────────────────────────────────────────


def _ar_model():
    """Two AR metrics on one snapshot: the local-currency balance and its EUR
    twin, named by label the way a controller says it."""
    from services.contracts import semantic_model as sm

    ar = sm.Entity(
        name="ar_snapshot",
        source={"connection": "warehouse", "table": "marts.ar_snapshot"},
        default_time_field="snapshot_day",
        fields=[
            sm.Field(name="snapshot_day", type="date"),
            sm.Field(name="currency", type="string"),
            sm.Field(name="open_balance", type="number"),
            sm.Field(name="open_balance_eur", type="number"),
            sm.Field(name="overdue", type="boolean"),
        ],
        metrics=[
            sm.Metric(name="open_ar", agg="sum", expr="ar_snapshot.open_balance"),
            sm.Metric(
                name="overdue_ar",
                agg="sum",
                expr="ar_snapshot.open_balance",
                filters=["ar_snapshot.overdue"],
            ),
            sm.Metric(
                name="overdue_ar_eur",
                agg="sum",
                expr="ar_snapshot.open_balance_eur",
                filters=["ar_snapshot.overdue"],
                label="overdue AR in EUR",
                aliases=["overdue receivables in euros"],
                currency="EUR",
            ),
        ],
    )
    return sm.SemanticModel(
        lens="finance",
        dialect="duckdb",
        entities=[ar],
        definitions=[
            Definition(term="dso", body="days sales outstanding", aliases=["collection days"])
        ],
    )


def test_labels_and_aliases_are_governed_names() -> None:
    from services.certify.lexical import declared_units, name_aliases

    model = _ar_model()
    aliases = name_aliases(model)
    assert aliases == {
        "overdue ar in eur": "overdue_ar_eur",
        "overdue receivables in euros": "overdue_ar_eur",
        "collection days": "dso",
    }
    names = governed_names(model)
    assert names_in("overdue AR in EUR at the end of August", names, aliases) == {
        "overdue_ar_eur",
        "overdue_ar",
    }
    assert names_in("what are our collection days", names, aliases) == {"dso"}
    # a label is a phrasing, matched whole: its words alone name nothing
    assert "overdue_ar_eur" not in names_in("overdue AR at month end", names, aliases)
    assert declared_units(model) == {"overdue_ar_eur": "eur"}


def test_a_sibling_named_by_its_label_is_a_different_governed_name() -> None:
    """The ledger shape: "overdue AR in EUR at the end of August 2026" against
    the template "open and overdue AR at {day} by currency", attributed to
    open_ar/overdue_ar. The label names overdue_ar_eur, which the template
    is not about — vetoed. The template's own wording still takes it."""
    from services.certify.lexical import name_aliases

    model = _ar_model()
    names, measures, aliases = governed_names(model), measure_names(model), name_aliases(model)
    approved = "open and overdue AR at {day} by currency"
    terms = frozenset({"open_balance", "open_ar", "overdue_ar"})
    assert lexical_mismatch(
        "overdue AR in EUR at the end of August 2026",
        approved,
        names,
        frozenset(),
        terms,
        measures,
        aliases,
    )
    assert lexical_mismatch(
        "overdue receivables in euros at 31 August",
        approved,
        names,
        frozenset(),
        terms,
        measures,
        aliases,
    )
    assert not lexical_mismatch(
        "open and overdue AR at 2026-08-31 by currency",
        approved,
        names,
        frozenset(),
        terms,
        measures,
        aliases,
    )
    # the label and the name are one metric: asked by label, approved by name
    assert not lexical_mismatch(
        "overdue AR in EUR at 31 August",
        "overdue_ar_eur at {day}",
        names,
        frozenset(),
        frozenset({"open_balance_eur", "overdue_ar_eur"}),
        measures,
        aliases,
    )


def test_the_unit_veto_reads_question_words_against_sql_and_declarations() -> None:
    from services.certify.lexical import unit_mismatch

    by_currency = (
        "SELECT currency, SUM(open_balance) AS open_ar FROM marts.ar_snapshot "
        "WHERE snapshot_day = {day} AND overdue GROUP BY currency"
    )
    # "by currency" reports local currencies; the question said EUR
    assert (
        unit_mismatch(
            "overdue AR in EUR at the end of August 2026",
            "open and overdue AR at {day} by currency",
            by_currency,
        )
        == "the question asks in eur and the certified answer reports local"
    )
    # a column named for the unit produces it
    assert (
        unit_mismatch(
            "overdue AR in EUR at the end of August 2026",
            "overdue AR at {day}",
            "SELECT SUM(open_balance_eur) FROM marts.ar_snapshot WHERE snapshot_day = {day}",
        )
        is None
    )
    # the lens's own declaration on the attributed metric counts both ways
    assert (
        unit_mismatch(
            "overdue AR in EUR",
            "overdue AR at {day}",
            "SELECT SUM(open_balance) FROM s",
            frozenset({"overdue_ar"}),
            {"overdue_ar": "sek"},
        )
        == "the question asks in eur and the certified answer reports sek"
    )
    assert (
        unit_mismatch(
            "overdue AR in EUR",
            "overdue AR at {day}",
            "SELECT SUM(open_balance) FROM s",
            frozenset({"overdue_ar"}),
            {"overdue_ar": "eur"},
        )
        is None
    )
    # other unit families, and the silences: a candidate that says nothing
    # about the unit decides nothing, a period word is not a unit, "local"
    # outside "local currency" is not a currency
    assert (
        unit_mismatch(
            "fuel in litres per shipment",
            "fuel per shipment",
            "SELECT SUM(fuel_gallons)/COUNT(*) FROM s",
        )
        == "the question asks in litres and the certified answer reports gallons"
    )
    assert (
        unit_mismatch(
            "median transit in hours",
            "median transit per shipment",
            "SELECT median(transit_minutes) FROM s",
        )
        == "the question asks in hours and the certified answer reports minutes"
    )
    assert (
        unit_mismatch(
            "on-time share in %",
            "on-time delivered count",
            "SELECT COUNT(*) FROM s",
            frozenset({"otd_count"}),
            {"otd_count": "eur"},
        )
        is None
    )
    assert (
        unit_mismatch("revenue in EUR this month", "revenue in {period}", "SELECT SUM(x) FROM s")
        is None
    )
    assert (
        unit_mismatch(
            "sales at the local hub in EUR", "sales by hub", "SELECT SUM(amount_eur) FROM s"
        )
        is None
    )
    assert (
        unit_mismatch("shipments per day", "shipments by month", "SELECT month, COUNT(*) FROM s")
        is None
    )


def test_in_eur_drops_the_by_currency_template_to_assisted(monkeypatch) -> None:
    """End to end through certified_lookup: the template binds and would have
    served; the unit veto keeps it off the EUR ask, and the EUR twin serves."""
    from services.certify.lexical import declared_units, name_aliases

    model = _ar_model()
    local = _Answer(
        id="ca_local",
        question="open and overdue AR at {day} by currency",
        sql=(
            "SELECT currency, SUM(open_balance) AS open_ar FROM marts.ar_snapshot "
            "WHERE snapshot_day = {day} AND overdue GROUP BY currency"
        ),
        slots={"day": "date"},
        sample_bindings=[{"day": "2026-08-31"}],
        terms=["open_balance", "open_ar", "overdue_ar"],
    )
    _wire(monkeypatch, [_Hit(local)])
    monkeypatch.setattr(assembly, "assist_llm", lambda: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(assembly, "certified_bind", lambda *_a, **_k: {"day": "2026-08-31"})
    kw = dict(
        governed=governed_names(model),
        measures=measure_names(model),
        aliases=name_aliases(model),
        units=declared_units(model),
    )
    top, _ex, mode, match, bound = certified_lookup(
        "finance", [0.1], "org", "overdue AR in EUR at the end of August 2026", **kw
    )
    assert top is None and match is None and bound is None and mode == "none"
    # the template's own ask, by currency, still binds and serves
    top, _ex, mode, match, bound = certified_lookup(
        "finance", [0.1], "org", "open and overdue AR at 2026-08-31 by currency", **kw
    )
    assert top is local and match == "parameterized" and bound == {"day": "2026-08-31"}
    # a plain hit whose SQL does not produce EUR folds as an exemplar instead
    plain = _Answer(
        id="ca_plain",
        question="overdue AR at the end of August 2026",
        sql="SELECT SUM(open_balance) AS overdue_ar FROM marts.ar_snapshot WHERE overdue",
        terms=["open_balance", "overdue_ar"],
    )
    _wire(monkeypatch, [_Hit(plain)])
    top, exemplars, mode, match, _b = certified_lookup(
        "finance", [0.1], "org", "overdue AR in EUR at the end of August 2026", **kw
    )
    assert top is None and mode == "assisted" and [e.sql for e in exemplars] == [plain.sql]
    # the EUR twin, named by label and producing EUR, serves exact
    twin = _Answer(
        id="ca_eur",
        question="overdue AR in EUR at the end of August 2026",
        sql="SELECT SUM(open_balance_eur) AS overdue_ar_eur FROM marts.ar_snapshot WHERE overdue",
        terms=["open_balance_eur", "overdue_ar_eur"],
    )
    _wire(monkeypatch, [_Hit(twin)])
    top, _ex, mode, match, _b = certified_lookup(
        "finance", [0.1], "org", "overdue AR in EUR at the end of August 2026", **kw
    )
    assert top is twin and mode == "certified" and match == "exact"
