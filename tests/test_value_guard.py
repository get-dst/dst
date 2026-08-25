"""The investigation reflex: a suspicious filter literal repairs instead of serving.

The failure: "sales in Finland" generates ``WHERE country = 'Finland'`` against
a column holding 'FI', and zero rows serve as a verified "no sales in Finland".
Pinned here, unit and end-to-end over a real DuckDB: a literal outside a
COMPLETE materialized domain repairs BEFORE execution (free, deterministic); with
no domain, a zero-row result buys one governed DISTINCT probe and repairs from
what it learns; a literal the column really holds is an HONEST zero and serves
unchanged with no repair consumed; and a stubborn generator cannot loop the
probe.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime import value_guard
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query

# ── fixtures ─────────────────────────────────────────────────────────────────


def _model(table: str = "customers") -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="warehouse", table=table),
                fields=[
                    Field(name="email", type="string"),
                    Field(name="country", type="string"),
                    Field(name="amount", type="number"),
                ],
            )
        ],
    )


def _profile(**col_kw: object) -> TableProfile:
    return TableProfile(
        connection="warehouse",
        table="customers",
        columns=[ColumnProfile(name="country", type="VARCHAR", **col_kw)],  # type: ignore[arg-type]
    )


# ── value_domains: what qualifies as a domain ────────────────────────────────


def test_only_complete_dictionaries_become_domains() -> None:
    complete = _profile(top_values=["FI", "DK"], values_complete=True)
    assert value_guard.value_domains(_model(), [complete]) == {"country": ["FI", "DK"]}
    partial = _profile(top_values=["FI", "DK"], values_complete=False)
    assert value_guard.value_domains(_model(), [partial]) == {}  # proves nothing


def test_conflicting_domains_across_entities_are_dropped() -> None:
    """A wrong domain in feedback is worse than none: `status` meaning one thing
    on orders and another on tickets must contribute nothing."""
    model = SemanticModel(
        lens="l",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="c", table="orders"),
                fields=[Field(name="status", type="string")],
            ),
            Entity(
                name="tickets",
                source=EntitySource(connection="c", table="tickets"),
                fields=[Field(name="status", type="string")],
            ),
        ],
    )
    profiles = [
        TableProfile(
            connection="c",
            table="orders",
            columns=[
                ColumnProfile(
                    name="status", type="VARCHAR", top_values=["paid"], values_complete=True
                )
            ],
        ),
        TableProfile(
            connection="c",
            table="tickets",
            columns=[
                ColumnProfile(
                    name="status", type="VARCHAR", top_values=["open"], values_complete=True
                )
            ],
        ),
    ]
    assert value_guard.value_domains(model, profiles) == {}


# ── unknown_literals: what triggers ──────────────────────────────────────────

DOMAINS = {"country": ["FI", "DK"]}


def test_a_literal_outside_the_domain_is_found_eq_in_and_reversed() -> None:
    eq = value_guard.unknown_literals(
        "SELECT * FROM customers WHERE country = 'Finland'", "duckdb", DOMAINS
    )
    assert [(m.column, m.literal) for m in eq] == [("country", "Finland")]
    isin = value_guard.unknown_literals(
        "SELECT * FROM customers WHERE country IN ('FI', 'Sweden')", "duckdb", DOMAINS
    )
    assert [(m.column, m.literal) for m in isin] == [("country", "Sweden")]
    reversed_ = value_guard.unknown_literals(
        "SELECT * FROM customers WHERE 'Finland' = country", "duckdb", DOMAINS
    )
    assert [(m.column, m.literal) for m in reversed_] == [("country", "Finland")]


def test_in_domain_unknown_column_and_unparseable_never_trigger() -> None:
    ok = "SELECT * FROM customers WHERE country = 'FI'"
    assert value_guard.unknown_literals(ok, "duckdb", DOMAINS) == []
    other = "SELECT * FROM customers WHERE city = 'Helsinki'"
    assert value_guard.unknown_literals(other, "duckdb", DOMAINS) == []
    assert value_guard.unknown_literals("not sql at all (", "duckdb", DOMAINS) == []


# ── empty_result_probe ───────────────────────────────────────────────────────


class _Recording(FakeConnector):
    def __init__(self, result: QueryResult) -> None:
        super().__init__(result=result)
        self.sqls: list[str] = []

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
        self.sqls.append(sql)
        return super().execute(sql, read_only=read_only, row_limit=row_limit)


def test_probe_reports_absent_literal_with_the_real_values() -> None:
    conn = _Recording(QueryResult(columns=["country"], rows=[["FI"], ["DK"]]))
    outcome = value_guard.empty_result_probe(
        "SELECT country FROM customers WHERE country = 'Finland'", _model(), conn
    )
    assert [(f.column, f.missing, f.values) for f in outcome.findings] == [
        ("country", ("Finland",), ("DK", "FI"))
    ]
    assert len(conn.sqls) == 1 and "DISTINCT" in conn.sqls[0]
    feedback = value_guard.probe_feedback(outcome.findings, "SELECT 1")
    assert "'FI'" in feedback and "'Finland'" in feedback


def test_a_present_literal_is_a_confirmed_honest_zero() -> None:
    conn = _Recording(QueryResult(columns=["country"], rows=[["FI"], ["DK"]]))
    outcome = value_guard.empty_result_probe(
        "SELECT country FROM customers WHERE country = 'FI'", _model(), conn
    )
    assert outcome.findings == () and outcome.confirmed and outcome.attempted


def test_unenumerable_and_failing_probes_are_attempted_but_unresolved() -> None:
    """Attempted-but-unresolved is the state that must NOT read as confirmed:
    the pipeline grades that zero below `verified` because nobody checked it."""
    wide = _Recording(QueryResult(columns=["country"], rows=[[str(i)] for i in range(26)]))
    outcome = value_guard.empty_result_probe(
        "SELECT country FROM customers WHERE country = 'x'", _model(), wide
    )
    assert outcome.findings == () and outcome.attempted and not outcome.confirmed

    class _Broken(FakeConnector):
        def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
            raise RuntimeError("connection reset")

    outcome = value_guard.empty_result_probe(
        "SELECT country FROM customers WHERE country = 'x'", _model(), _Broken()
    )
    assert outcome.findings == () and outcome.attempted and not outcome.confirmed


# ── zero_evidence: the one-row aggregate costume ─────────────────────────────


def test_zero_evidence_sees_through_the_aggregate_costume() -> None:
    """COUNT over an empty set is 0 and SUM is NULL — one row that `has_rows`
    grades as evidence. This is the confident-zero the whole track exists for."""
    zero = QueryResult(columns=["n"], rows=[[0]])
    assert value_guard.zero_evidence("SELECT count(*) AS n FROM t", "duckdb", zero)
    mixed = QueryResult(columns=["n", "total"], rows=[[0, None]])
    assert value_guard.zero_evidence(
        "SELECT count(*) AS n, sum(amount) AS total FROM t", "duckdb", mixed
    )


def test_zero_evidence_never_fires_on_real_evidence() -> None:
    assert not value_guard.zero_evidence(
        "SELECT count(*) AS n FROM t", "duckdb", QueryResult(columns=["n"], rows=[[5]])
    )
    # a non-aggregate projection can legitimately be 0/NULL — bail
    assert not value_guard.zero_evidence(
        "SELECT amount FROM t", "duckdb", QueryResult(columns=["amount"], rows=[[0]])
    )
    # grouped aggregates: one group of zero is a real group, not an empty match
    assert not value_guard.zero_evidence(
        "SELECT sum(x) FROM t GROUP BY y", "duckdb", QueryResult(columns=["s"], rows=[[None]])
    )
    # wrapped aggregates bail (under-detection over a false positive)
    assert not value_guard.zero_evidence(
        "SELECT round(avg(x), 2) FROM t", "duckdb", QueryResult(columns=["a"], rows=[[None]])
    )


# ── the pipeline, end to end over a real warehouse ───────────────────────────


def _warehouse(tmp_path: Path) -> DuckDBConnector:
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE customers (email VARCHAR, country VARCHAR, amount DECIMAL(10,2))")
    con.execute(
        "INSERT INTO customers VALUES ('ada@x','FI',100.0), ('bo@x','DK',250.0), ('cy@x','FI',80.0)"
    )
    con.close()
    return DuckDBConnector(str(tmp_path / "wh.duckdb"))


def _gen(*sqls: str) -> GroundedSQLGenerator:
    return GroundedSQLGenerator(ScriptedLLM(['{"sql": "' + s + '"}' for s in sqls]))


def test_a_known_domain_repairs_before_the_warehouse_is_touched(tmp_path: Path) -> None:
    """A 'Finland' filter against a ['FI','DK'] domain regenerates into a domain
    literal on the first retry and the answer serves rows, not a confident zero."""
    res = run_query(
        question="sales in Finland",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(
            "SELECT country, amount FROM customers WHERE country = 'Finland'",
            "SELECT country, amount FROM customers WHERE country = 'FI'",
        ),
        composer=None,
        value_domains={"country": ["FI", "DK"]},
    )
    assert res.trace.status == "ok" and res.trace.repairs == 1
    assert res.response.sql is not None and "'FI'" in res.response.sql
    assert res.response.data is not None and len(res.response.data.rows) == 2


def test_no_domain_probes_the_empty_result_then_repairs(tmp_path: Path) -> None:
    """Zero rows, no stored domain — one governed DISTINCT probe
    learns 'FI'/'DK', the feedback repairs the filter, the answer serves."""
    res = run_query(
        question="sales in Finland",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(
            "SELECT country, amount FROM customers WHERE country = 'Finland'",
            "SELECT country, amount FROM customers WHERE country = 'FI'",
        ),
        composer=None,
    )
    assert res.trace.status == "ok" and res.trace.repairs == 1
    assert res.response.data is not None and len(res.response.data.rows) == 2
    assert "probe" in res.trace.latency_ms  # the investigation is on the trace


def test_an_honest_zero_serves_without_consuming_a_repair(tmp_path: Path) -> None:
    """The literal exists; the zero is real (another predicate emptied it). The
    probe proves the literal present and the zero serves — investigation must
    never turn an honest empty answer into churn."""
    res = run_query(
        question="big sales in Finland",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(
            "SELECT country, amount FROM customers WHERE country = 'FI' AND amount > 99999",
        ),
        composer=None,
    )
    assert res.trace.status == "ok" and res.trace.repairs == 0
    assert res.response.data is not None and res.response.data.rows == []


def test_a_proven_absent_value_clarifies_instead_of_serving_the_zero(tmp_path: Path) -> None:
    """Probe path: 'Mars' repairs once; the generator repeats the
    same SQL, its cached findings outlive the repair budget, and the answer is an
    unknown_value clarification naming the stored values — never the accidental
    zero, and never a probe loop."""
    res = run_query(
        question="sales on Mars",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen("SELECT country, amount FROM customers WHERE country = 'Mars'"),
        composer=None,
    )
    assert res.trace.status == "clarification" and res.response.status == "clarification"
    clar = res.response.clarification
    assert clar is not None and clar.kind == "unknown_value" and clar.term == "country"
    assert set(clar.options) == {"FI", "DK"} and "'Mars'" in clar.question


def test_a_known_domain_miss_that_repair_cannot_fix_clarifies(tmp_path: Path) -> None:
    """Domain path: the domain is COMPLETE, so executing a
    persistent miss could only dress the mismatch up as data — ask instead."""
    res = run_query(
        question="sales on Mars",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen("SELECT country, amount FROM customers WHERE country = 'Mars'"),
        composer=None,
        value_domains={"country": ["FI", "DK"]},
    )
    assert res.response.status == "clarification"
    clar = res.response.clarification
    assert clar is not None and clar.kind == "unknown_value" and clar.options == ["FI", "DK"]
    # the guard asked before the warehouse ever saw the bad literal, and the one
    # probe the pipeline is allowed never ran
    assert "probe" not in res.trace.latency_ms


def test_a_count_zero_costume_is_investigated_and_repaired(tmp_path: Path) -> None:
    """COUNT(*) over an empty match returns one row holding 0 —
    `has_rows` passes it, so it used to serve as verified. Now it
    triggers the same investigation as zero rows, and repairs into the answer."""
    res = run_query(
        question="how many sales in Finland",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(
            "SELECT count(*) AS n FROM customers WHERE country = 'Finland'",
            "SELECT count(*) AS n FROM customers WHERE country = 'FI'",
        ),
        composer=None,
    )
    assert res.trace.status == "ok" and res.trace.repairs == 1
    assert res.response.data is not None and res.response.data.rows == [[2]]
    check = res.response.verification and res.response.verification.check(
        "empty_result_investigation"
    )
    assert check is None  # the served result has rows-with-evidence; nothing to attest


def test_a_confirmed_zero_keeps_verified_an_unresolved_one_never_gets_it(
    tmp_path: Path,
) -> None:
    """The grade half: an investigated-and-confirmed COUNT zero keeps
    its badge (the absence IS the answer); a zero nobody could check is capped at
    `partial` — which is exactly what auto_review's "partial" net catches."""
    confirmed = run_query(
        question="how many big sales in Finland",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(),
        connector=_warehouse(tmp_path),
        generator=_gen(
            "SELECT count(*) AS n FROM customers WHERE country = 'FI' AND amount > 99999"
        ),
        composer=None,
    )
    assert confirmed.trace.status == "ok" and confirmed.trace.repairs == 0
    assert confirmed.response.confidence == "verified"
    check = confirmed.response.verification and confirmed.response.verification.check(
        "empty_result_investigation"
    )
    assert check is not None and check.status == "pass"

    unresolved = run_query(
        question="how many sales in Atlantis",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_wide_model(),
        connector=_wide_warehouse(tmp_path),
        generator=_gen("SELECT count(*) AS n FROM customers WHERE city = 'Atlantis'"),
        composer=None,
    )
    assert unresolved.trace.status == "ok"
    assert unresolved.response.confidence == "partial"  # never verified: nobody checked it
    check = unresolved.response.verification and unresolved.response.verification.check(
        "empty_result_investigation"
    )
    assert check is not None and check.status == "fail"


def _wide_model() -> SemanticModel:
    model = _model()
    entity = model.entities[0]
    fields = [*entity.fields, Field(name="city", type="string")]
    return model.model_copy(update={"entities": [entity.model_copy(update={"fields": fields})]})


def _wide_warehouse(tmp_path: Path) -> DuckDBConnector:
    con = duckdb.connect(str(tmp_path / "wide.duckdb"))
    con.execute(
        "CREATE TABLE customers (email VARCHAR, country VARCHAR, amount DECIMAL(10,2), "
        "city VARCHAR)"
    )
    con.execute(
        "INSERT INTO customers SELECT 'u' || i, 'FI', 10.0, 'city' || i FROM range(30) t(i)"
    )
    con.close()
    return DuckDBConnector(str(tmp_path / "wide.duckdb"))
