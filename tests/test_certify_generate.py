"""lens-ux: generate certified question→SQL from governed definitions, with a verified
value captured by running the SQL read-only, stored so the serving path uses it."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from services.certify import store
from services.certify.generate import generate_for_lens
from services.config import settings
from services.contracts.fakes import HashEmbedder, ScriptedLLM
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot
from services.db.session import org_session


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)

_SM = SemanticModel.model_validate(
    {
        "version": 1,
        "lens": "genlens",
        "dialect": "duckdb",
        "entities": [
            {
                "name": "rev",
                "source": {"connection": "d", "table": "revenue"},
                "fields": [{"name": "net_revenue", "type": "number"}],
            }
        ],
        "definitions": [
            {
                "term": "monthly revenue",
                "body": "Net revenue per month.",
                "sql_expr": "SUM(net_revenue)",
                "source": "authored",
            }
        ],
    }
)


class _Conn:
    """A connector that runs the generated SQL to a fixed scalar (the verified value)."""

    kind = "duckdb"

    def introspect(self) -> SchemaSnapshot:
        return SchemaSnapshot(connection="d", dialect=self.kind)

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        return QueryResult(columns=["v"], rows=[[42]])


@needs_db
def test_generate_certified_runs_and_stores_with_a_verified_value() -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Gen') RETURNING id")).scalar_one()
    drafted = '{"question": "monthly revenue?", "sql": "SELECT SUM(net_revenue) AS v FROM revenue"}'
    llm = ScriptedLLM([drafted])
    try:
        with org_session(org) as s:
            results = generate_for_lens(
                s,
                lens="genlens",
                semantic_model=_SM,
                connector=_Conn(),
                embedder=HashEmbedder(),
                llm=llm,
                model_name="scripted",
            )
        (r,) = results
        assert r.term == "monthly revenue"
        assert r.id is not None  # stored
        assert r.error is None
        assert r.verified_value == {"value": 42}  # the SQL was run for a real value

        # it lands in the certified-answer store the serving path consults
        with org_session(org) as s:
            stored = store.list_for_lens(s, "genlens")
        assert [a.question for a in stored] == ["monthly revenue?"]
        assert stored[0].verified_value == {"value": 42}
        assert stored[0].created_by == "generated"
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM certified_answer WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_generate_skips_a_metric_whose_sql_fails_the_guard() -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Gen2') RETURNING id")).scalar_one()
    # SELECT * is rejected by the guard → not stored, surfaced as an error.
    llm = ScriptedLLM(['{"question": "rev?", "sql": "SELECT * FROM revenue"}'])
    try:
        with org_session(org) as s:
            (r,) = generate_for_lens(
                s,
                lens="genlens2",
                semantic_model=_SM,
                connector=_Conn(),
                embedder=HashEmbedder(),
                llm=llm,
                model_name="scripted",
            )
        assert r.id is None
        assert r.error is not None and "guard" in r.error
        with org_session(org) as s:
            assert store.list_for_lens(s, "genlens2") == []
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM certified_answer WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
