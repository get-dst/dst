"""sql_guard rejects out-of-scope / non-SELECT SQL; in-scope passes."""

from __future__ import annotations

import pytest

from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime import sql_guard


def _model() -> SemanticModel:
    # `customers` deliberately exposes a SUBSET of columns to test column scope.
    return SemanticModel(
        lens="customer_value",
        dialect="duckdb",
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="jaffle", table="customers"),
                fields=[
                    Field(name="customer_id", type="integer"),
                    Field(name="customer_lifetime_value", type="number"),
                    Field(name="number_of_orders", type="integer"),
                ],
            ),
            Entity(
                name="orders",
                source=EntitySource(connection="jaffle", table="orders"),
                fields=[
                    Field(name="order_id", type="integer"),
                    Field(name="customer_id", type="integer"),
                    Field(name="amount", type="number"),
                    Field(name="order_month", type="date"),
                ],
            ),
        ],
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_id, customer_lifetime_value FROM customers",
        "SELECT count(*) AS n FROM customers",
        "SELECT c.customer_id, o.amount FROM customers c"
        " JOIN orders o ON c.customer_id = o.customer_id",
        "SELECT customer_id FROM (SELECT customer_id FROM customers) sub",
        "SELECT customer_id FROM customers -- trailing comment is harmless",
        # A LATERAL join's target is a derived relation, not a table name — the
        # tables INSIDE it are still allow-listed (false rejection observed:
        # "table '' is out of lens scope").
        "SELECT c.customer_id, l.amount FROM customers c"
        " LEFT JOIN LATERAL (SELECT o.amount FROM orders o"
        " WHERE o.customer_id = c.customer_id LIMIT 1) l ON TRUE",
        "SELECT c.customer_id, x.amount FROM customers c,"
        " LATERAL (SELECT o.amount FROM orders o WHERE o.customer_id = c.customer_id) x",
        # A row-generating function (the date-spine idiom) reads nothing, so the
        # table allow-list has no jurisdiction over it.
        "SELECT d.m FROM generate_series(1, 12) AS d(m)",
        "WITH months AS (SELECT m FROM generate_series(1, 12) AS t(m))"
        " SELECT months.m, count(o.order_id) AS n FROM months"
        " LEFT JOIN orders o ON o.order_month = months.m GROUP BY 1",
        # A column COMPUTED in a CTE, then read back qualified by the CTE name —
        # which here also names a physical table. It is a projection of the CTE,
        # not a physical column (false rejection observed: "column
        # 'customer_transactions.min_month' is out of lens scope").
        "WITH orders AS (SELECT customer_id, min(order_month) AS min_month"
        " FROM main.orders GROUP BY 1)"
        " SELECT orders.customer_id, orders.min_month FROM orders",
        "SELECT s.total FROM (SELECT customer_id, sum(amount) AS total FROM orders GROUP BY 1) s",
        # A star CONFINED to a CTE/subquery reaches no caller: the outermost
        # projection names the columns, so nothing unmodeled comes back. The
        # whole-statement star scan this replaced refused the CTE-staging idiom
        # ("I couldn't form an in-scope query: SELECT * is not allowed").
        "WITH staged AS (SELECT * FROM customers)"
        " SELECT customer_id, customer_lifetime_value FROM staged",
        "SELECT s.customer_id FROM (SELECT * FROM customers) s",
        "WITH staged AS (SELECT * FROM orders)"
        " SELECT customer_id FROM customers UNION SELECT customer_id FROM staged",
        # A QUALIFIED star confined to an inner scope is the same idiom with a
        # table prefix: `o.*` parses as Column(this=Star), and the column walk
        # used to read it as a column NAME ("column 'bt.*' is out of lens
        # scope"). The outer projection still
        # names what comes back, and the table under the qualifier is still
        # allow-listed like any other.
        "WITH staged AS (SELECT o.* FROM orders o) SELECT customer_id FROM staged",
        "SELECT s.customer_id FROM (SELECT c.* FROM customers c) s",
        "WITH staged AS (SELECT o.* FROM main.orders AS o) SELECT customer_id FROM staged",
    ],
)
def test_in_scope_allowed(sql: str) -> None:
    r = sql_guard.check(sql, _model())
    assert r.ok, r.reason
    assert r.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",  # bare star
        "SELECT c.* FROM customers c",  # table star
        # A star in the OUTERMOST projection reaches the caller in every shape
        # the outermost projection can take — over a derived relation, over a
        # CTE, qualified, parenthesized, or in either branch of a set operation.
        "SELECT * FROM (SELECT customer_id FROM customers) s",
        "WITH x AS (SELECT customer_id FROM customers) SELECT * FROM x",
        "WITH x AS (SELECT * FROM customers) SELECT * FROM x",
        "SELECT x.* FROM (SELECT customer_id FROM customers) x",
        "(SELECT * FROM customers)",
        "SELECT customer_id FROM customers UNION SELECT * FROM orders",
        "SELECT * FROM customers UNION SELECT customer_id FROM orders",
        # ...and an aggregate over a qualified star still hands back every column.
        "SELECT array_agg(c.*) FROM customers c",
        "SELECT id FROM raw_payments",  # out-of-scope table
        "SELECT customer_id FROM customers UNION SELECT id FROM raw_payments",  # union
        "SELECT customers.first_order_date FROM customers",  # out-of-scope column
        "DELETE FROM customers",  # DML
        "UPDATE customers SET customer_lifetime_value = 0",  # DML
        "DROP TABLE customers",  # DDL
        "SELECT 1; DROP TABLE customers",  # multi-statement
        "WITH x AS (DELETE FROM customers RETURNING customer_id)"
        " SELECT customer_id FROM x",  # DML in CTE
        # CTE shadowing the bare name must not hide a QUALIFIED reference to
        # the physical table (a CTE can never be schema-qualified)
        "WITH raw_payments AS (SELECT 1 AS amount)"
        " SELECT sum(p.amount) FROM main.raw_payments AS p",
        # A function target is only waved through when it GENERATES rows; one that
        # reads a data source is a table the lens never modeled.
        "SELECT t.a FROM read_csv('/etc/passwd') AS t",
        "SELECT t.a FROM read_parquet('s3://someone-elses/bucket') AS t",
        "SELECT t.a FROM dblink('host=evil', 'select 1') AS t(a)",
        # Per-scope column resolution must not become a shadowing loophole: the
        # FROM binds `customers` to the PHYSICAL table, so the unmodeled column
        # is still out of scope even though a CTE shares the name.
        "WITH customers AS (SELECT 1 AS a) SELECT customers.first_order_date FROM main.customers",
        # ...nor may a qualifier no scope binds (the table name kept after an
        # alias — duckdb accepts it) fall through unchecked.
        "SELECT customers.first_order_date FROM customers c",
        # ...nor may a correlated reference into an outer scope escape it.
        "SELECT c.customer_id FROM customers c"
        " WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.first_order_date)",
    ],
)
def test_out_of_scope_rejected(sql: str) -> None:
    r = sql_guard.check(sql, _model())
    assert not r.ok
    assert r.reason


def test_empty_sql_says_the_generator_produced_nothing() -> None:
    # A reasoning model that spends its budget on thinking returns "". sqlglot
    # parses that to zero statements, and the old "exactly one statement is
    # allowed" blamed the SQL for a query that was never produced — the message
    # hid a total-failure bug.
    for sql in ("", "   \n\t ", "-- I could not answer this\n"):
        r = sql_guard.check(sql, _model())
        assert not r.ok
        assert "no SQL" in (r.reason or "")
        assert "statement" not in (r.reason or "")


def test_multi_statement_keeps_its_own_message() -> None:
    r = sql_guard.check("SELECT customer_id FROM customers; SELECT 1", _model())
    assert not r.ok
    assert r.reason == "exactly one statement is allowed"


def test_column_scope_falls_back_when_scopes_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scope resolver is best-effort; a shape sqlglot cannot resolve must fall
    # back to the flat alias map, never to skipping the column check.
    def _boom(_expression: object) -> None:
        raise ValueError("unresolvable")

    monkeypatch.setattr(sql_guard, "build_scope", _boom)
    r = sql_guard.check("SELECT customers.first_order_date FROM customers", _model())
    assert not r.ok
    assert r.reason == "column 'customers.first_order_date' is out of lens scope"


def test_star_in_a_cte_is_not_a_way_into_an_out_of_scope_table() -> None:
    # The security pin on allowing inner stars: the table walk, not the star
    # rule, is what keeps unmodeled tables unreadable — assert on ITS reason, so
    # a future star relaxation cannot quietly widen the table boundary.
    r = sql_guard.check("WITH x AS (SELECT * FROM raw_payments) SELECT id FROM x", _model())
    assert not r.ok
    assert r.reason == "table 'raw_payments' is out of lens scope"

    r = sql_guard.check("SELECT s.case_id FROM (SELECT * FROM __dst.eval_expected) s", _model())
    assert not r.ok
    assert "__dst" in (r.reason or "")

    # The QUALIFIED star takes the same door: `bt.*` over an out-of-scope table
    # is stopped by the table walk, with the table's reason (the projection rule
    # column walk's reading of Column(this=Star), never the table boundary).
    r = sql_guard.check("WITH x AS (SELECT bt.* FROM raw_payments bt) SELECT id FROM x", _model())
    assert not r.ok
    assert r.reason == "table 'raw_payments' is out of lens scope"


def test_outermost_qualified_star_keeps_the_star_reason() -> None:
    # `c.*` in what the CALLER receives is still the star rule's rejection —
    # asserted by REASON so the inner-scope relaxation can never creep
    # into the outermost projection.
    r = sql_guard.check("SELECT c.* FROM customers c", _model())
    assert not r.ok
    assert r.reason == "SELECT * is not allowed; select explicit columns"


def test_star_scan_falls_back_to_the_whole_statement_on_an_unclassifiable_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outer-vs-inner is decided structurally; a root shape the walk cannot
    # classify must fail closed onto the old whole-statement scan, never fall
    # through to allowing a star that might reach the caller.
    monkeypatch.setattr(sql_guard, "_output_projections", lambda _stmt: None)
    r = sql_guard.check("WITH x AS (SELECT * FROM customers) SELECT customer_id FROM x", _model())
    assert not r.ok
    assert "SELECT *" in (r.reason or "")


def test_a_deep_set_operation_gets_a_verdict_instead_of_a_crash() -> None:
    # Branch count is caller-controlled, so the outer-projection walk must be
    # iterative: the recursive version raised RecursionError
    # straight out of check() at ~1000 UNION branches — a security boundary
    # answers, it does not crash. The star in the last branch is still caught.
    deep = " UNION ".join(["SELECT customer_id FROM customers"] * 1200)
    assert sql_guard.check(deep, _model()).ok
    r = sql_guard.check(deep + " UNION SELECT * FROM orders", _model())
    assert not r.ok
    assert "SELECT *" in (r.reason or "")


# ── unqualified in-scope relations are rewritten source-qualified ────────────


def _spider_model() -> SemanticModel:
    # The trap class: entity name == bare table
    # name, source schema-qualified. `FROM bitcoin_prices` passed the allow-list
    # untouched and DuckDB declined the request with "Table with name
    # bitcoin_prices does not exist! Did you mean spider.bitcoin_prices?".
    def entity(name: str, fields: list[str]) -> Entity:
        return Entity(
            name=name,
            source=EntitySource(connection="wh", table=f"spider.{name}"),
            fields=[Field(name=f, type="string") for f in fields],
        )

    return SemanticModel(
        lens="bitcoin",
        dialect="duckdb",
        entities=[
            entity("bitcoin_prices", ["market_date", "price"]),
            entity("bitcoin_transactions", ["member_id", "txn_date", "qty"]),
            entity("bitcoin_members", ["member_id", "region"]),
        ],
    )


def test_p17_unqualified_relations_are_rewritten_to_their_sources(tmp_path) -> None:
    from services.connectors.duckdb import DuckDBConnector

    # A decline shape: multiple CTEs, 2 of the 3 relations written
    # unqualified, the third already source-qualified. The guard knows every
    # entity's source table, so it rewrites instead of letting the request die.
    sql = (
        "WITH daily AS (SELECT market_date, price FROM bitcoin_prices),"
        " spend AS (SELECT t.member_id, t.txn_date, t.qty FROM bitcoin_transactions t)"
        " SELECT m.region, d.price, s.qty"
        " FROM spend s"
        " JOIN daily d ON d.market_date = s.txn_date"
        " JOIN spider.bitcoin_members m ON m.member_id = s.member_id"
    )
    r = sql_guard.check(sql, _spider_model())
    assert r.ok, r.reason
    assert r.sql is not None
    assert "spider.bitcoin_prices" in r.sql and "spider.bitcoin_transactions" in r.sql
    # The proof is execution: as written the SQL raises the live catalog
    # error, the guard's rewrite answers.
    import duckdb

    path = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA spider")
    con.execute("CREATE TABLE spider.bitcoin_prices (market_date DATE, price DOUBLE)")
    con.execute(
        "CREATE TABLE spider.bitcoin_transactions (member_id VARCHAR, txn_date DATE, qty INTEGER)"
    )
    con.execute("CREATE TABLE spider.bitcoin_members (member_id VARCHAR, region VARCHAR)")
    con.execute("INSERT INTO spider.bitcoin_prices VALUES ('2021-01-01', 29374.15)")
    con.execute("INSERT INTO spider.bitcoin_transactions VALUES ('m1', '2021-01-01', 2)")
    con.execute("INSERT INTO spider.bitcoin_members VALUES ('m1', 'europe')")
    con.close()
    with pytest.raises(Exception, match="does not exist"):
        DuckDBConnector(path).execute(sql)
    assert DuckDBConnector(path).execute(r.sql).rows == [["europe", 29374.15, 2]]


def test_p17_cte_references_are_never_rewritten() -> None:
    # A recursive CTE carrying an entity's very name: its self-reference is the
    # CTE, not the warehouse table — rewriting it would turn the recursion into
    # a physical scan.
    sql = (
        "WITH RECURSIVE bitcoin_prices AS ("
        "SELECT 1 AS n UNION ALL SELECT n + 1 FROM bitcoin_prices WHERE n < 3)"
        " SELECT n FROM bitcoin_prices"
    )
    r = sql_guard.check(sql, _spider_model())
    assert r.ok, r.reason
    assert r.sql is not None and "spider" not in r.sql
    # ...and an entity-named CTE shadows the outer reference the same way:
    # only the reference already inside the CTE is source-qualified.
    sql = (
        "WITH bitcoin_prices AS (SELECT market_date FROM spider.bitcoin_prices)"
        " SELECT market_date FROM bitcoin_prices"
    )
    r = sql_guard.check(sql, _spider_model())
    assert r.ok, r.reason
    assert r.sql is not None and r.sql.count("spider.") == 1


def test_p17_subquery_alias_is_not_a_relation_target() -> None:
    # A derived-relation alias — even one colliding with an entity name — is
    # never rewritten; the table INSIDE the subquery still is. Exactly one
    # source-qualified reference comes out.
    sql = "SELECT bitcoin_prices.price FROM (SELECT price FROM bitcoin_prices) AS bitcoin_prices"
    r = sql_guard.check(sql, _spider_model())
    assert r.ok, r.reason
    assert r.sql is not None and r.sql.count("spider.bitcoin_prices") == 1


def test_p17_already_qualified_tables_pass_as_written() -> None:
    sql = "SELECT price FROM spider.bitcoin_prices"
    r = sql_guard.check(sql, _spider_model())
    assert r.ok, r.reason
    assert r.sql == sql


def test_p17_ambiguous_bare_name_stays_untouched() -> None:
    # `events` is the bare table name of BOTH raw.events and analytics.events:
    # the guard must not guess a schema. The older behavior is preserved — in
    # scope, passed through exactly as written (the warehouse's own resolution,
    # or its error, is the honest outcome). The entity NAMES stay unambiguous
    # and still resolve.
    model = SemanticModel(
        lens="events",
        dialect="duckdb",
        entities=[
            Entity(
                name="web_events",
                source=EntitySource(connection="wh", table="raw.events"),
                fields=[Field(name="event_id", type="string")],
            ),
            Entity(
                name="app_events",
                source=EntitySource(connection="wh", table="analytics.events"),
                fields=[Field(name="event_id", type="string")],
            ),
        ],
    )
    r = sql_guard.check("SELECT event_id FROM events", model)
    assert r.ok, r.reason
    assert r.sql is not None and "raw." not in r.sql and "analytics." not in r.sql
    r = sql_guard.check("SELECT web_events.event_id FROM web_events", model)
    assert r.ok, r.reason
    assert r.sql is not None and "raw.events AS web_events" in r.sql


# ── a keyword name written bare is delimited BEFORE the parse ────────────────


def _reserved_model(table: str, entity: str) -> SemanticModel:
    return SemanticModel(
        lens="fleet_lens",
        dialect="duckdb",
        entities=[
            Entity(
                name=entity,
                source=EntitySource(connection="wh", table=table),
                fields=[
                    Field(name="select", type="string"),
                    Field(name="order", type="string"),
                ],
            )
        ],
    )


def test_a_reserved_table_in_the_default_schema_executes(tmp_path) -> None:
    """The differential: a reserved table in a NAMED schema
    served (entity `analytics_select` is not a keyword), and the same table in the
    DEFAULT schema — where the entity name IS the keyword — did not. Quoting after
    the parse cannot reach it: `FROM select` reads as `From(this=Select())`, so
    there is no identifier to delimit and no table for the allow-list to see, and
    the statement rendered back out as `FROM SELECT` and died in the warehouse."""
    import duckdb

    from services.connectors.duckdb import DuckDBConnector

    model = _reserved_model("select", "select")
    sql = "SELECT count(*) AS fleet_volume FROM select"
    r = sql_guard.check(sql, model)
    assert r.ok, r.reason
    assert r.sql == 'SELECT COUNT(*) AS fleet_volume FROM "select"'

    path = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(path)
    con.execute('CREATE TABLE "select" ("select" VARCHAR, "order" VARCHAR)')
    con.execute("INSERT INTO \"select\" VALUES ('a', 'b'), ('c', 'd')")
    con.close()
    with pytest.raises(Exception, match="[Ss]yntax error"):
        DuckDBConnector(path).execute(sql)
    assert DuckDBConnector(path).execute(r.sql).rows == [[2]]


def test_a_keyword_qualifier_on_a_column_is_delimited_too() -> None:
    # The second shape the same variant failed on — a hard ParseError, not a
    # silent collapse: `select."select"` never reached the allow-list at all.
    r = sql_guard.check(
        'SELECT select."select", select.order FROM select', _reserved_model("select", "select")
    )
    assert r.ok, r.reason
    assert r.sql == 'SELECT "select"."select", "select"."order" FROM "select"'


def test_the_named_schema_variant_still_resolves_the_way_it_did() -> None:
    # The variant that already passed must keep passing: the model addresses the
    # entity by its safe slug and the guard rewrites it to the qualified table.
    r = sql_guard.check(
        "SELECT count(*) AS fleet_volume FROM analytics_select",
        _reserved_model("analytics.select", "analytics_select"),
    )
    assert r.ok, r.reason
    assert r.sql == 'SELECT COUNT(*) AS fleet_volume FROM analytics."select" AS analytics_select'


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_id FROM customers",
        "SELECT CAST(customer_lifetime_value AS INT) AS v FROM customers",
        "SELECT 'select' AS lit, customer_id FROM customers",
        "SELECT customer_id FROM (SELECT customer_id FROM customers) t",
    ],
)
def test_a_lens_owning_no_keyword_is_returned_byte_for_byte(sql: str) -> None:
    # The pre-parse pass touches only names the lens owns AND that are keywords,
    # so an ordinary lens never tokenizes and never changes — the same promise the
    # emission side of identifiers.py makes.
    import sqlglot

    r = sql_guard.check(sql, _model())
    assert r.ok, r.reason
    assert r.sql == sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")


def test_validation_schema_is_never_queryable() -> None:
    # Defense in depth: even when the table *name* is in the lens's allow-list, a
    # reference into the reserved __dst schema must be rejected outright (a lens
    # can never read its own eval fixtures). See architecture.md "Deployment posture".
    for sql in (
        "SELECT case_id FROM __dst.eval_expected",
        "SELECT customer_id FROM __dst.customers",  # allowed name, reserved schema
    ):
        r = sql_guard.check(sql, _model())
        assert not r.ok
        assert "__dst" in (r.reason or "")


def test_real_cast_is_widened_to_double() -> None:
    # REAL is 4-byte single precision in DuckDB, Postgres and Snowflake, so the same
    # ratio computes to a different number than DOUBLE: identical questions answer
    # 0.8113590478897095 instead of 0.8113590263691683 purely because the
    # generated SQL said CAST(... AS REAL).
    # Widened, never rejected: DOUBLE represents every REAL exactly, so nothing means
    # anything different afterwards and no repair round-trip is spent.
    r = sql_guard.check(
        "SELECT CAST(SUM(number_of_orders) AS REAL) / COUNT(*) AS ratio FROM customers",
        _model(),
    )
    assert r.ok, r.reason
    assert r.sql is not None
    assert "REAL" not in r.sql.upper()
    assert "CAST(SUM(number_of_orders) AS DOUBLE)" in r.sql


def test_try_cast_to_real_is_widened_too() -> None:
    # The dialect notes actively tell the model to prefer TRY_CAST on text columns,
    # so the narrow cast arrives in that spelling just as often.
    r = sql_guard.check(
        "SELECT TRY_CAST(customer_lifetime_value AS REAL) AS v FROM customers", _model()
    )
    assert r.ok, r.reason
    assert r.sql is not None and "DOUBLE" in r.sql and "REAL" not in r.sql.upper()


def test_wide_and_exact_casts_pass_untouched() -> None:
    # The negative twin: only single precision is rewritten. DOUBLE and NUMERIC are
    # already correct, and a cast to a non-numeric type is none of this rule's business.
    for cast, expected in (
        ("CAST(customer_lifetime_value AS DOUBLE)", "DOUBLE"),
        ("CAST(customer_lifetime_value AS NUMERIC)", "DECIMAL"),
        # sqlglot has always rendered duckdb VARCHAR as TEXT — that normalization is
        # the round-trip's, not this rule's, and the point here is that it is unchanged.
        ("CAST(customer_id AS VARCHAR)", "TEXT"),
    ):
        r = sql_guard.check(f"SELECT {cast} AS v FROM customers", _model())
        assert r.ok, r.reason
        assert r.sql is not None and expected in r.sql.upper()


@pytest.mark.parametrize(
    ("dialect", "rendered"),
    [
        ("duckdb", "DOUBLE"),
        ("postgres", "DOUBLE PRECISION"),
        ("snowflake", "DOUBLE"),
        ("bigquery", "FLOAT64"),
        ("mysql", "DOUBLE"),
    ],
)
def test_widening_renders_each_dialect_own_spelling(dialect: str, rendered: str) -> None:
    model = _model().model_copy(update={"dialect": dialect})
    r = sql_guard.check("SELECT CAST(customer_lifetime_value AS REAL) AS v FROM customers", model)
    assert r.ok, r.reason
    assert r.sql is not None and rendered in r.sql.upper()


def test_definition_authored_with_real_still_reads_as_applied() -> None:
    # The interaction the widening creates, pinned: a definition whose sql_expr says
    # CAST(... AS REAL) must still match the DOUBLE the guard now serves, or a
    # precision fix would silently fail definition_applied and cost the grade it was
    # meant to protect.
    from services.contracts.semantic_model import Definition
    from services.contracts.warehouse import QueryResult
    from services.runtime.verification import build_report

    model = _model().model_copy(
        update={
            "definitions": [
                Definition(
                    term="order_rate",
                    body="orders per customer",
                    sql_expr="CAST(SUM(number_of_orders) AS REAL) / COUNT(*)",
                )
            ]
        }
    )
    served = sql_guard.check(
        "SELECT CAST(SUM(number_of_orders) AS REAL) / COUNT(*) AS ratio FROM customers", model
    )
    assert served.ok and served.sql is not None and "REAL" not in served.sql.upper()
    report = build_report(
        question="orders per customer?",
        sql=served.sql,
        answer_text="Customers place 1.5 orders each.",
        result=QueryResult(columns=["ratio"], rows=[[1.5]]),
        semantic_model=model,
        definition_used="order_rate",
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "pass", applied


# ─── dialect-invalid functions die at the guard, as SYNTAX ───────────────────


def _bq_model() -> SemanticModel:
    return SemanticModel(
        lens="revops",
        dialect="bigquery",
        entities=[
            Entity(
                name="funnel",
                source=EntitySource(connection="bq", table="proj.marts.funnel"),
                fields=[
                    Field(name="full_date", type="date"),
                    Field(name="sqls", type="number"),
                    Field(name="sales_motion", type="string"),
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    ("fragment", "fix"),
    [
        ("YEAR(funnel.full_date) = 2026", "EXTRACT(YEAR FROM"),
        ("MONTH(funnel.full_date) = 8", "EXTRACT(MONTH FROM"),
        ("DATE_FORMAT(funnel.full_date, '%Y') = '2026'", "FORMAT_DATE"),
        ("funnel.full_date = CURDATE()", "CURRENT_DATE"),
    ],
)
def test_dialect_invalid_functions_are_syntax_faults_with_the_fix_named(
    fragment: str, fix: str
) -> None:
    """sqlglot parses YEAR(d) for bigquery and re-renders it unchanged, so the
    engine 400s on it and the dry-run failure was misfiled as a data-team
    ticket. The guard now catches the
    class before any warehouse round trip — kind='syntax' routes it through
    the generation-fault repair machinery, and the message carries the fix."""
    sql = (
        "SELECT funnel.sales_motion, SUM(funnel.sqls) AS s FROM proj.marts.funnel AS funnel "
        f"WHERE {fragment} GROUP BY funnel.sales_motion"
    )
    r = sql_guard.check(sql, _bq_model())
    assert not r.ok and r.kind == "syntax"
    assert r.reason is not None and fix in r.reason and "not a scope problem" in r.reason


def test_the_valid_bigquery_form_passes() -> None:
    sql = (
        "SELECT funnel.sales_motion, SUM(funnel.sqls) AS s FROM proj.marts.funnel AS funnel "
        "WHERE EXTRACT(YEAR FROM funnel.full_date) = 2026 GROUP BY funnel.sales_motion"
    )
    assert sql_guard.check(sql, _bq_model()).ok


def test_year_stays_legal_on_engines_that_have_it() -> None:
    # duckdb HAS year() — the deny-list is per-dialect, never a blanket ban.
    sql = "SELECT COUNT(*) AS n FROM orders WHERE YEAR(orders.order_month) = 2026"
    assert sql_guard.check(sql, _model()).ok


def test_datediff_is_not_denied_because_sqlglot_transpiles_it() -> None:
    sql = (
        "SELECT DATEDIFF(funnel.full_date, funnel.full_date) AS d FROM proj.marts.funnel AS funnel"
    )
    r = sql_guard.check(sql, _bq_model())
    # transpiled to DATE_DIFF(…, …, DAY) at render — no reason to refuse
    assert r.ok and r.sql is not None and "DATE_DIFF" in r.sql


# ── one BigQuery table-quoting style ─────────────────────────────────────────
# The prompt taught `proj`.dataset.table (quoted part by part) while dst's own
# sampling SQL wrote `proj.dataset.table` (wrapped whole). The model mixed them —
# opening per part and closing the path — and emitted `proj`.dataset.table`,
# a stray trailing backtick that made an answerable question a syntax error on
# every run. A path with any quoted part is now wrapped whole, so there is no
# half-quoted shape left to complete.


@pytest.mark.parametrize(
    ("table", "dialect", "expected"),
    [
        ("data-platform.finance.kpis", "bigquery", "`data-platform.finance.kpis`"),
        ("proj.marts.orders", "bigquery", "proj.marts.orders"),  # nothing needs quoting
        ("marts.order", "bigquery", "`marts.order`"),  # reserved word, still whole
        # MySQL is the other backtick dialect and must NOT wrap: there
        # `db.tbl` names ONE table containing a dot, not a qualified path.
        ("my-db.tbl", "mysql", "`my-db`.tbl"),
        ("analytics.orders", "postgres", "analytics.orders"),
    ],
)
def test_a_qualified_path_is_quoted_in_one_style(table: str, dialect: str, expected: str) -> None:
    from services.runtime.identifiers import quote_table

    assert quote_table(table, dialect) == expected


@pytest.mark.parametrize(
    ("table", "dialect"),
    [
        ("data-platform.finance.kpis", "bigquery"),
        ("marts.order", "bigquery"),
        ("my-db.tbl", "mysql"),
        ("analytics.orders", "postgres"),
    ],
)
def test_the_emitted_form_still_parses_back_to_the_same_table(table: str, dialect: str) -> None:
    """The allow-list, the filter guard and the bindings all compare PARSED
    parts — a quoting change that moved them would be a governance change."""
    import sqlglot
    from sqlglot import exp

    from services.runtime.identifiers import quote_table

    parsed = sqlglot.parse_one(f"SELECT 1 FROM {quote_table(table, dialect)}", read=dialect)
    found = parsed.find(exp.Table)
    assert found is not None
    assert ".".join(p for p in (found.catalog, found.db, found.name) if p) == table


def test_the_stray_trailing_backtick_is_still_caught(_=None) -> None:
    """The emitter fix removes the exemplar that taught it; the guard stays the
    backstop, because the model can still write one."""
    sql = (
        "SELECT SUM(kpis.arr) AS arr FROM `data-platform.finance.kpis` AS kpis "
        "WHERE kpis.d = (SELECT MAX(d) FROM `data-platform.finance.kpis`)`"
    )
    r = sql_guard.check(sql, _bq_model())
    assert not r.ok and r.kind == "syntax"
    assert "unbalanced backtick" in (r.reason or "")


# ── a qualified reference is checked as written ──────────────────────────────
# The allow-list compared LAST SEGMENTS, so `other-project.marts.orders` passed a
# lens modelling `acme-prod.marts.orders` — same table name, different project —
# and the rewritten SQL kept the foreign path. A service account with access to
# both (the ordinary BigQuery case) read another project's data through a
# governed lens. Found where the catalog-stamp work meets the quoting work.


def _bq_qualified_model() -> SemanticModel:
    from services.contracts.semantic_model import Entity, EntitySource, Field

    return SemanticModel(
        lens="rev",
        dialect="bigquery",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="acme-prod.marts.orders"),
                fields=[Field(name="amount", type="number")],
            )
        ],
    )


@pytest.mark.parametrize(
    ("table", "allowed"),
    [
        ("`acme-prod.marts.orders`", True),  # exactly what the lens models
        ("marts.orders", True),  # omits the catalog — same table
        ("orders", True),  # bare — the entity's own name
        ("`other-project.marts.orders`", False),  # contradicts the project
        ("`acme-prod.other_dataset.orders`", False),  # contradicts the dataset
    ],
)
def test_a_qualified_table_is_scoped_by_what_it_names(table: str, allowed: bool) -> None:
    sql = f"SELECT SUM(o.amount) AS revenue FROM {table} AS o"
    result = sql_guard.check(sql, _bq_qualified_model())
    assert result.ok is allowed, result.reason
    if not allowed:
        assert "out of lens scope" in (result.reason or "")


def test_certified_sql_still_reaches_its_own_physical_tables() -> None:
    """`trust_tables` is the certified contract: a human approved the exact SQL,
    so it may name tables no entity exposes. Tightening the generated path must
    not tighten that one."""
    sql = "SELECT SUM(o.amount) AS revenue FROM `other-project.marts.orders` AS o"
    assert sql_guard.check(sql, _bq_qualified_model(), trust_tables=True).ok
