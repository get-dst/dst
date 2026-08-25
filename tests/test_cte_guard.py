"""The binder guarantee: a CTE must project what its consumers reference.

The fault: `WITH danish AS (SELECT acct_id, account_name …)` later joined as
`danish AS a ON a.external_ref = …`. The engine's binder error burns the single
repair attempt without landing the fix; this guard catches the same fact
pre-warehouse and names the CTE to edit, the column to add, and the projection
list that exists.

Bias contract (filter_guard's): under-detection acceptable, false positives
forbidden — and wiring makes false positives harmless anyway (the guard only
spends repair attempts; exhausted, the SQL flows to the warehouse unchanged).
"""

from __future__ import annotations

from services.runtime.cte_guard import repair_feedback, underprojected_cte_refs

# The failing SQL, verbatim shape (trimmed to the failing half).
BURST_SQL = (
    "WITH danish AS (SELECT acct_id, account_name FROM crm.accounts "
    "WHERE country_name = 'Denmark'), "
    "order_intake AS (SELECT a.acct_id, COALESCE(SUM(o.BELOEB / f.KURS), 0) AS oi "
    "FROM danish AS a "
    "LEFT JOIN erp.customers AS c ON a.external_ref = c.KUNDE_NR "
    "LEFT JOIN erp.orders AS o ON c.KUNDE_NR = o.KUNDE_NR "
    "LEFT JOIN erp.fx_rate AS f ON o.VALUTA = f.VALUTA "
    "GROUP BY a.acct_id) "
    "SELECT d.account_name, oi.oi FROM danish AS d "
    "JOIN order_intake AS oi ON d.acct_id = oi.acct_id"
)


def test_the_burst_fault_is_caught_pre_warehouse() -> None:
    misses = underprojected_cte_refs(BURST_SQL, "duckdb")
    assert [(m.cte, m.alias, m.column) for m in misses] == [("danish", "a", "external_ref")]
    assert misses[0].projected == ("acct_id", "account_name")


def test_feedback_names_the_cte_to_edit_not_the_alias() -> None:
    """`a.external_ref` locates the violation, but there is no CTE `a` to
    edit — a repair told only about `a` fixes nothing."""
    msg = repair_feedback(underprojected_cte_refs(BURST_SQL, "duckdb"), BURST_SQL)
    assert "CTE `danish`" in msg
    assert "`a.external_ref`" in msg
    assert "acct_id, account_name" in msg  # the list that IS projected


def test_a_projected_reference_is_clean() -> None:
    sql = "WITH t AS (SELECT a, b FROM x) SELECT t.a, t.b FROM t"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_aliased_projection_counts_as_projected() -> None:
    sql = "WITH t AS (SELECT x.raw AS clean FROM x) SELECT t.clean FROM t"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_star_cte_is_never_flagged() -> None:
    """`SELECT *` output can't be enumerated without schema — under-detect."""
    sql = "WITH t AS (SELECT * FROM x) SELECT t.anything FROM t"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_reference_case_does_not_matter() -> None:
    sql = "WITH t AS (SELECT acct_id FROM x) SELECT T.ACCT_ID FROM t AS T"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_same_name_table_in_another_scope_is_not_confused() -> None:
    """Scope-awareness: `s` is a subquery here, not the CTE — no flag."""
    sql = "WITH t AS (SELECT a FROM x) SELECT s.b FROM (SELECT b FROM y) AS s JOIN t ON t.a = s.b"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_unqualified_columns_are_skipped() -> None:
    """No qualifier, no certainty about the source — under-detect."""
    sql = "WITH t AS (SELECT a FROM x) SELECT external_ref FROM t"
    assert underprojected_cte_refs(sql, "duckdb") == []


def test_duplicate_references_report_once() -> None:
    sql = (
        "WITH t AS (SELECT a FROM x) SELECT t.missing FROM t WHERE t.missing > 0 ORDER BY t.missing"
    )
    assert len(underprojected_cte_refs(sql, "duckdb")) == 1


def test_unparseable_sql_is_silent() -> None:
    assert underprojected_cte_refs("WITH t AS (SELEC", "duckdb") == []
