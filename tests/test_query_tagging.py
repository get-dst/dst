"""Every dst-authored query identifies itself — the tagging seam.

The tag is a structured leading comment that survives in warehouse query history:
warehouse admins get attribution for free, and the drift audit mechanically
excludes self-traffic (including its own variant re-runs).
"""

from datetime import UTC, datetime

from services.connectors.duckdb import DuckDBConnector
from services.connectors.tagging import MARKER, query_context, tag_sql
from services.contracts.query_history import QueryRecord
from services.probe.drift import mine_drift


def test_bare_tag_has_app_only() -> None:
    tagged = tag_sql("SELECT 1")
    assert tagged.startswith(MARKER)
    assert '"app": "dst"' in tagged
    assert tagged.endswith("SELECT 1")


def test_context_fields_render_and_nest() -> None:
    with query_context(purpose="serve", lens="sales_comp", caller="alex"):
        tagged = tag_sql("SELECT 1")
        assert '"purpose": "serve"' in tagged
        assert '"lens": "sales_comp"' in tagged
        assert '"caller": "alex"' in tagged
        with query_context(purpose="eval"):  # inner overrides, outer fields survive
            inner = tag_sql("SELECT 1")
            assert '"purpose": "eval"' in inner
            assert '"lens": "sales_comp"' in inner
    assert '"purpose"' not in tag_sql("SELECT 1")  # context restored on exit


def test_tagging_is_idempotent() -> None:
    once = tag_sql("SELECT 1")
    assert tag_sql(once) == once


def test_none_values_drop() -> None:
    with query_context(purpose="serve", lens=None):
        assert '"lens"' not in tag_sql("SELECT 1")


def test_connector_prepends_the_tag_to_the_final_statement(tmp_path, monkeypatch) -> None:
    """The connector is the tagging seam: the FINAL statement (row-cap wrapper
    included) goes to the engine with the marker as its leading comment, and
    the comment is valid SQL. The spy records exactly what the engine ran."""
    import duckdb

    from services.connectors import duckdb as duckdb_connector

    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t AS SELECT 1 AS n UNION ALL SELECT 2")
    con.close()
    sent: list[str] = []
    real = duckdb_connector.tag_sql

    def spy(query: str) -> str:
        tagged = real(query)
        sent.append(tagged)
        return tagged

    monkeypatch.setattr(duckdb_connector, "tag_sql", spy)
    connector = DuckDBConnector(str(db))
    with query_context(purpose="serve", lens="l"):
        result = connector.execute("SELECT SUM(n) AS total FROM t", row_limit=5)
    assert result.rows == [[3]]
    (statement,) = sent
    assert statement.lstrip().startswith(MARKER)
    assert '"purpose": "serve"' in statement and '"lens": "l"' in statement
    assert "LIMIT 5" in statement  # the tag wraps the FINAL query, cap included


def test_miner_excludes_dst_self_traffic() -> None:
    """Tagged history records never mine as organic practice — a governed query
    comes from the compiled model and cannot be definition drift.

    The tagged record here is a THIRD variant of the SAME metric intent as the
    two organic ones: without the exclusion it would cluster with them and
    surface as a variant. The finding must carry exactly the two organic
    variants — that is the exclusion observed, not assumed."""

    def rec(statement: str, runs: int) -> QueryRecord:
        return QueryRecord(
            statement=statement,
            first_seen=datetime(2026, 5, 1, tzinfo=UTC),
            last_seen=datetime(2026, 6, 1, tzinfo=UTC),
            run_count=runs,
        )

    organic = [
        rec("SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly", 5),
        rec("SELECT SUM(revenue_eur) AS total FROM gold.fct_revenue_monthly", 3),
    ]
    with query_context(purpose="serve", lens="rev"):
        tagged = [
            rec(
                tag_sql("SELECT SUM(gross_revenue_eur) AS total FROM gold.fct_revenue_monthly"),
                40,
            )
        ]
    # Sanity for the setup itself: untagged, the same statement DOES cluster —
    # so the assertion below can only pass because of the exclusion.
    untagged_control = [
        rec("SELECT SUM(gross_revenue_eur) AS total FROM gold.fct_revenue_monthly", 40)
    ]
    (control,) = mine_drift(organic + untagged_control)
    assert len(control.variants) == 3
    (finding,) = mine_drift(organic + tagged)
    assert len(finding.variants) == 2
    assert all(MARKER not in v.statement for v in finding.variants)
