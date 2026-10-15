"""Two rails over the lens's table scope — declared names, deterministic matches.

The named-table rail: "how many events per day" on a lens with ``orders`` and no
events table served ``order_count`` as events — a silent noun swap on the
generation path. Pinned here: a named table the SQL does not read clarifies and
names both tables; a named table the SQL reads serves; a question naming no
table serves; a table another lens holds is named with that lens; the regular
plural names the table; the typed path is untouched.

The scoped-suggestion rail: a warehouse error's ``Did you mean "staging.orders"?``
once reached the caller naming a table outside the lens's grant. Pinned here: an
out-of-scope suggestion is dropped, an in-scope one kept, the closest in-scope
name substituted, a column suggestion kept to the allowed columns, the admin
door untouched, and the rest of the error text verbatim.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import BackgroundTasks, HTTPException

from services.api import sql as sql_door
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.governance.credentials import CallerIdentity
from services.lenses.demo import jaffle_customer_value_bundle
from services.runtime import scope_rails
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query

# ── fixtures ─────────────────────────────────────────────────────────────────


def _model(customer_entity: str = "customers") -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="warehouse", table="analytics.orders"),
                fields=[
                    Field(name="order_id", type="string"),
                    Field(name="order_date", type="date"),
                    Field(name="amount", type="number"),
                ],
            ),
            Entity(
                name=customer_entity,
                source=EntitySource(connection="warehouse", table="analytics.customers"),
                fields=[
                    Field(name="customer_id", type="string"),
                    Field(name="country", type="string"),
                ],
            ),
        ],
    )


ORDERS_PER_DAY = "SELECT order_date, count(*) AS n FROM analytics.orders GROUP BY order_date"
CUSTOMER_COUNT = "SELECT count(*) AS n FROM analytics.customers"


def _gen(sql: str) -> GroundedSQLGenerator:
    return GroundedSQLGenerator(ScriptedLLM([f'{{"sql": "{sql}", "rationale": "r"}}']))


def _run(question: str, sql: str, **kw: object):  # noqa: ANN202 — PipelineResult
    return run_query(
        question=question,
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=kw.pop("semantic_model", _model()),  # type: ignore[arg-type]
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[3]])),
        generator=kw.pop("generator", _gen(sql)),  # type: ignore[arg-type]
        composer=None,
        **kw,  # type: ignore[arg-type]
    )


# ── rail 1: a named table the SQL does not read ──────────────────────────────


def test_a_named_table_the_sql_does_not_read_clarifies_naming_both() -> None:
    res = _run("how many customers ordered last week", ORDERS_PER_DAY)
    assert res.response.status == "clarification" and res.trace.status == "clarification"
    clar = res.response.clarification
    assert clar is not None and clar.kind == "named_table" and clar.term == "customers"
    assert clar.options == ["orders", "customers"]
    assert "you named `customers`" in clar.question
    assert "answer from `orders`" in clar.question
    assert "did you mean orders, or the `customers` table?" in clar.question
    assert res.response.answer == clar.question and res.response.sql is None
    assert res.trace.clarification == clar


def test_a_named_table_the_sql_reads_serves() -> None:
    res = _run("how many orders per day", ORDERS_PER_DAY)
    assert res.response.status == "ok" and res.response.clarification is None


def test_a_question_naming_no_table_serves() -> None:
    res = _run("how many per day in the last 7 days", ORDERS_PER_DAY)
    assert res.response.status == "ok"


def test_a_table_another_lens_holds_is_named_with_that_lens() -> None:
    looked_up: list[int] = []

    def elsewhere() -> dict[str, str]:
        looked_up.append(1)
        return {"events": "explore", "staging.events": "explore"}

    res = _run(
        "how many events per day in the last 7 days", ORDERS_PER_DAY, tables_elsewhere=elsewhere
    )
    assert res.response.status == "clarification"
    clar = res.response.clarification
    assert clar is not None and clar.kind == "named_table" and clar.term == "events"
    assert clar.question == (
        "you named `events`; this lens would answer from `orders` — did you mean "
        "orders, or the `events` table (lens `explore` holds it)?"
    )
    assert looked_up == [1]


def test_the_regular_plural_names_the_table() -> None:
    # the entity is `customer`; "customers" names it, as named_values reads a
    # lowercase value's plural
    res = _run("how many customers ordered", ORDERS_PER_DAY, semantic_model=_model("customer"))
    clar = res.response.clarification
    assert clar is not None and clar.term == "customers" and clar.options == ["orders", "customer"]
    # ...and the bare name serves when the SQL reads the table
    res = _run("count every customer", CUSTOMER_COUNT, semantic_model=_model("customer"))
    assert res.response.status == "ok"


def test_the_typed_path_is_untouched() -> None:
    class _Typed:
        model = "typed"

        def generate(self, **_kw: object) -> GeneratedQuery:
            return GeneratedQuery(sql=ORDERS_PER_DAY, typed=True)

    res = _run("how many customers ordered last week", "", generator=_Typed())
    assert res.response.status == "ok" and res.response.clarification is None


def test_a_common_word_table_name_still_names_the_table() -> None:
    # "orders" in "purchase orders" is the table — in the normal case the SQL
    # reads it; only the mismatch clarifies
    assert (
        scope_rails.named_table_clarification("purchase orders by week", _model(), ORDERS_PER_DAY)
        is None
    )
    clar = scope_rails.named_table_clarification(
        "purchase orders by week", _model(), CUSTOMER_COUNT
    )
    assert clar is not None and clar.term == "orders"


def test_a_table_named_after_the_named_one_counts_as_read() -> None:
    # "staged orders" answered from stg_orders: the read table's own name says
    # it is the orders table, so nothing fires; the same for a dim_/fct_ prefix
    # and the singular
    m = _model()
    m.entities.append(
        Entity(
            name="stg_orders",
            source=EntitySource(connection="warehouse", table="staging.stg_orders"),
            fields=[Field(name="order_id", type="string")],
            exposed=True,
        )
    )
    staged = "SELECT count(*) AS n FROM staging.stg_orders"
    assert scope_rails.named_table_clarification("how many staged orders", m, staged) is None
    m.entities.append(
        Entity(
            name="dim_customer",
            source=EntitySource(connection="warehouse", table="marts.dim_customer"),
            fields=[Field(name="customer_id", type="string")],
        )
    )
    dim = "SELECT count(*) AS n FROM marts.dim_customer"
    assert scope_rails.named_table_clarification("how many customers", m, dim) is None
    # naming the staging table and being served the mart is the swap
    clar = scope_rails.named_table_clarification("rows in stg_orders", m, ORDERS_PER_DAY)
    assert clar is not None and clar.term == "stg_orders"


def test_a_column_named_after_the_named_table_counts_as_reading_it() -> None:
    # "customers with more than one order" counted over customers.number_of_orders
    # reads orders through a column that says so; "revenue by customer" reads
    # customers through orders.customer_id
    m = _model()
    m.entities[1].fields.append(Field(name="number_of_orders", type="number"))
    counted = "SELECT count(*) AS n FROM analytics.customers WHERE number_of_orders > 1"
    assert scope_rails.named_table_clarification("customers with over 1 orders", m, counted) is None
    by_customer = "SELECT customer_id, sum(amount) FROM analytics.orders GROUP BY customer_id"
    assert scope_rails.named_table_clarification("revenue by customers", m, by_customer) is None
    # a bare count of customers answers nothing about orders
    assert scope_rails.named_table_clarification("customers with orders", m, CUSTOMER_COUNT)


def test_qualified_names_match_as_written_and_unknown_schemas_do_not() -> None:
    m = _model()
    assert (
        scope_rails.named_table_clarification("rows in analytics.orders", m, ORDERS_PER_DAY) is None
    )
    clar = scope_rails.named_table_clarification("rows in analytics.orders", m, CUSTOMER_COUNT)
    assert clar is not None and clar.term == "analytics.orders"
    # a schema the lens does not hold names nothing — no guess at the bare tail
    assert (
        scope_rails.named_table_clarification("rows in staging.orders", m, CUSTOMER_COUNT) is None
    )
    # a case-insensitive match, as the engine reads identifiers
    assert scope_rails.named_table_clarification("Orders by day", m, CUSTOMER_COUNT) is not None


# ── rail 2: suggestions only from tables in scope ────────────────────────────

DUCK_TABLE = (
    'Catalog Error: Table with name ordes does not exist!\nDid you mean "{s}"?\n\n'
    "LINE 1: select * from ordes\n                      ^"
)


def test_an_out_of_scope_suggestion_is_dropped_and_the_rest_stays_verbatim() -> None:
    out = scope_rails.scoped_suggestions(DUCK_TABLE.format(s="raw.secrets"), _model())
    # the closest in-scope name to what was written ("ordes") replaces it
    assert out == DUCK_TABLE.format(s="orders")
    # nothing in scope is close to the reference: the sentence goes, nothing else
    far = DUCK_TABLE.replace("ordes", "zzzzzz").format(s="raw.zzzzzz")
    assert scope_rails.scoped_suggestions(far, _model()) == (
        "Catalog Error: Table with name zzzzzz does not exist!\n\n"
        "LINE 1: select * from zzzzzz\n                      ^"
    )


def test_an_in_scope_suggestion_is_kept_as_written() -> None:
    text = DUCK_TABLE.format(s="analytics.orders")
    assert scope_rails.scoped_suggestions(text, _model()) == text
    bare = DUCK_TABLE.format(s="orders")
    assert scope_rails.scoped_suggestions(bare, _model()) == bare


def test_the_closest_in_scope_name_is_substituted() -> None:
    text = 'Table with name custmers does not exist!\nDid you mean "staging.customers"?'
    assert scope_rails.scoped_suggestions(text, _model()) == (
        'Table with name custmers does not exist!\nDid you mean "customers"?'
    )


def test_column_suggestions_are_kept_to_the_allowed_columns() -> None:
    duck = 'Binder Error: Referenced column "amnt" not found in FROM clause!\nCandidate bindings: '
    assert scope_rails.scoped_suggestions(duck + '"orders.amnt_raw"', _model()) == duck + '"amount"'
    pg = 'column "amnt" does not exist\nHINT:  Perhaps you meant to reference the column '
    assert scope_rails.scoped_suggestions(pg + '"staging.orders.amount".', _model()) == (
        pg + '"amount".'
    )
    bq = "Unrecognized name: amnt; Did you mean amount_hidden? at [1:8]"
    assert scope_rails.scoped_suggestions(bq, _model()) == (
        "Unrecognized name: amnt; Did you mean amount? at [1:8]"
    )
    # nothing close in scope: the sentence and its separator go
    assert scope_rails.scoped_suggestions(
        "Unrecognized name: zzz; Did you mean qqq? at [1:8]", _model()
    ) == ("Unrecognized name: zzz at [1:8]")


def test_errors_with_no_suggestion_and_function_hints_are_untouched() -> None:
    plain = "Out of Memory Error: could not allocate block"
    assert scope_rails.scoped_suggestions(plain, _model()) == plain
    fn = (
        'Catalog Error: Scalar Function with name dateadd does not exist!\nDid you mean "date_add"?'
    )
    assert scope_rails.scoped_suggestions(fn, _model()) == fn


class _Failing(FakeConnector):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        raise RuntimeError(self._message)


def test_the_pipeline_scrubs_the_warehouse_error_it_shows_and_keeps_it_on_the_trace() -> None:
    raw = DUCK_TABLE.format(s="raw.secrets")
    res = run_query(
        question="how many orders",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_Failing(raw),
        generator=_gen("SELECT count(*) AS n FROM analytics.orders"),
        composer=None,
        max_repairs=0,
    )
    assert res.response.status == "error" and res.error_kind == "warehouse"
    assert res.response.answer == "The query could not be executed: " + DUCK_TABLE.format(
        s="orders"
    )
    assert res.trace.error == raw


def _door(monkeypatch: pytest.MonkeyPatch, *, connector: FakeConnector, admin: bool) -> str:
    bundle = jaffle_customer_value_bundle()
    monkeypatch.setattr(sql_door, "_authorized_bundle", lambda *_a, **_k: bundle)
    monkeypatch.setattr(sql_door, "_authorized_connector", lambda *_a, **_k: connector)
    monkeypatch.setattr(sql_door, "_connection_connector", lambda *_a, **_k: connector)
    monkeypatch.setattr(sql_door, "log_trace", lambda *_a, **_k: None)
    caller = CallerIdentity(org_id=uuid.uuid4(), name="t", is_admin=admin)
    body = (
        sql_door.SqlBody(sql="SELECT customer_id FROM customers", connection="jaffle")
        if admin
        else sql_door.SqlBody(sql="SELECT customer_id FROM customers", lens="customer_value")
    )
    with pytest.raises(HTTPException) as err:
        sql_door._run(body, BackgroundTasks(), caller)
    assert err.value.status_code == 400
    return str(err.value.detail)


def test_the_sql_door_scopes_suggestions_on_the_lens_door_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        'Catalog Error: Table with name custmers does not exist!\nDid you mean "staging.customers"?'
    )
    assert _door(monkeypatch, connector=_Failing(raw), admin=False) == (
        "warehouse error: Catalog Error: Table with name custmers does not exist!\n"
        'Did you mean "customers"?'
    )
    # the admin probes the whole connection: no lens scope to keep to
    assert _door(monkeypatch, connector=_Failing(raw), admin=True) == f"warehouse error: {raw}"
