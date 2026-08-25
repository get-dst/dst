"""DuckDB connector — the local, file-backed warehouse.

Read-only by construction: every QUERY connection is opened with ``read_only=True``,
so writes/DDL raise regardless of the ``read_only`` argument. The SQL-level SELECT-only
guarantee is additionally enforced by sql_guard. The single exception is
``probe_write``, which exists to prove write access and reopens the file writable —
it is reached only when a connection is registered asking for ``access: ["write"]``,
never from serving or from ``dst apply``.

Introspection covers EVERY user schema, not the current one: ``SHOW TABLES``
(and a ``schema_name = 'main'`` filter in the catalog pass) makes a warehouse
whose tables live in any other schema look empty — a blank line and exit 0, the
worst failure shape there is. Names are qualified ``schema.table`` except in
``main``, which DuckDB resolves unqualified.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import duckdb

from services.connectors.sampling import sample_tables
from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    ColumnProfile,
    JoinCandidate,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.query_history import QueryRecord
from services.contracts.warehouse import (
    VALIDATION_SCHEMA,
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)

DEFAULT_SCHEMA = "main"

# Tables AND views (``SHOW TABLES`` listed both; jaffle's stg_* models are views),
# every user schema, no system relations — ``internal`` is the catalog's own flag
# for those, so nothing is matched by name.
_RELATIONS = (
    "SELECT schema_name, table_name, comment, FALSE AS is_view FROM duckdb_tables() "
    "WHERE NOT internal "
    "UNION ALL "
    "SELECT schema_name, view_name, comment, TRUE FROM duckdb_views() WHERE NOT internal "
    "ORDER BY 1, 2"
)
_COLUMNS = (
    "SELECT schema_name, table_name, column_name, data_type, is_nullable, comment "
    "FROM duckdb_columns() WHERE NOT internal ORDER BY schema_name, table_name, column_index"
)
# The catalog pass sees exactly what introspect() lists — views included. Reading
# duckdb_tables() alone made every relation a view unprofilable, and every dbt
# staging model is a view: they were listed for authoring and could never carry a
# profile fact. Views have no estimated_size — introspect()'s own count(*) is
# what puts a row count next to them in the listing.
_CATALOG_RELATIONS = (
    "SELECT schema_name, table_name, estimated_size, comment, FALSE AS is_view "
    "FROM duckdb_tables() WHERE NOT internal "
    "UNION ALL "
    "SELECT schema_name, view_name, NULL::BIGINT, comment, TRUE FROM duckdb_views() "
    "WHERE NOT internal "
    "ORDER BY 1, 2"
)
# `main` is flagged internal in duckdb_schemas(), so the user's schemas are
# "everything in an attached database, minus dst's own validation plane".
_SCHEMAS = (
    "SELECT DISTINCT schema_name FROM duckdb_schemas() "
    "WHERE database_name NOT IN ('system', 'temp') AND schema_name != ? ORDER BY 1"
)


def qualified(schema: str, table: str) -> str:
    """``schema.table``, except in ``main`` — DuckDB resolves an unqualified name
    against ``main`` only, so its tables stay copy-pasteable into SQL and a lens
    can keep addressing ``customers`` unqualified."""
    return table if schema == DEFAULT_SCHEMA else f"{schema}.{table}"


class DuckDBConnector:
    kind = "duckdb"

    def __init__(self, path: str, *, profile: bool = True, schema: str | None = None) -> None:
        self._path = path
        self._profile = profile
        self._schema = schema  # None = every user schema

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(self._path, read_only=True)

    def _searched_schemas(self, con: duckdb.DuckDBPyConnection) -> list[str]:
        if self._schema:
            return [self._schema]
        return [str(s) for (s,) in con.execute(_SCHEMAS, [VALIDATION_SCHEMA]).fetchall()]

    def _in_scope(self, schema: str) -> bool:
        return schema != VALIDATION_SCHEMA and (self._schema is None or schema == self._schema)

    def introspect(self) -> SchemaSnapshot:
        con = self._connect()
        try:
            columns: dict[tuple[str, str], list[ColumnSchema]] = {}
            for sname, tname, cname, ctype, nullable, comment in con.execute(_COLUMNS).fetchall():
                if not self._in_scope(str(sname)):
                    continue
                profile: dict[str, object] | None = None
                if self._profile:
                    relation = f'"{sname}"."{tname}"'
                    distinct = con.execute(
                        f'SELECT count(DISTINCT "{cname}") FROM {relation}'
                    ).fetchone()
                    # counted, not estimated — the listing may print it as fact
                    profile = {
                        "distinct": int(distinct[0]) if distinct else 0,
                        "distinct_exact": True,
                    }
                columns.setdefault((str(sname), str(tname)), []).append(
                    ColumnSchema(
                        name=str(cname),
                        type=str(ctype),
                        nullable=bool(nullable),
                        description=str(comment) if comment else None,
                        profile=profile,
                    )
                )
            tables: list[TableSchema] = []
            for sname, tname, comment, is_view in con.execute(_RELATIONS).fetchall():
                if not self._in_scope(str(sname)):
                    continue
                rc = con.execute(f'SELECT count(*) FROM "{sname}"."{tname}"').fetchone()
                tables.append(
                    TableSchema(
                        name=qualified(str(sname), str(tname)),
                        columns=columns.get((str(sname), str(tname)), []),
                        row_count=int(rc[0]) if rc else None,
                        is_view=bool(is_view),
                        description=str(comment) if comment else None,
                    )
                )
            return SchemaSnapshot(
                connection=self._path,
                dialect="duckdb",
                tables=tables,
                schemas_searched=self._searched_schemas(con),
            )
        finally:
            con.close()

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile from duckdb_tables()/duckdb_views()/duckdb_columns() —
        no table scans.

        Row counts are the catalog's estimates (views have none); the database
        file's mtime is the best-effort physical freshness.
        """
        modified = self._file_modified()
        con = self._connect()
        try:
            table_rows = con.execute(_CATALOG_RELATIONS).fetchall()
            col_rows = con.execute(_COLUMNS).fetchall()
        finally:
            con.close()
        columns: dict[str, list[ColumnProfile]] = {}
        for sname, tname, cname, ctype, nullable, comment in col_rows:
            if not self._in_scope(str(sname)):
                continue
            columns.setdefault(qualified(str(sname), str(tname)), []).append(
                ColumnProfile(
                    name=str(cname),
                    type=str(ctype),
                    nullable=bool(nullable),
                    description=str(comment) if comment else None,
                    description_source="warehouse" if comment else None,
                )
            )
        now = datetime.now(UTC)
        return [
            TableProfile(
                connection=self._path,
                table=qualified(str(sname), str(tname)),
                description=str(comment) if comment else None,
                row_count=int(size) if size is not None else None,
                is_view=bool(is_view),
                last_updated_physical=modified,
                profiled_at=now,
                source="catalog",
                columns=columns.get(qualified(str(sname), str(tname)), []),
            )
            for sname, tname, size, comment, is_view in table_rows
            if self._in_scope(str(sname))
        ]

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """DuckDB's catalog declares no FKs we collect; join inference covers it."""
        return []

    def _file_modified(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(os.stat(self._path).st_mtime, tz=UTC)
        except OSError:  # in-memory database or a missing file
            return None

    # ── SamplingProfiler ──────────────────────────────────────────────────────

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Guarded sampling pass: ``USING SAMPLE``-capped stats, read-only."""
        return sample_tables(
            self.execute,
            tables,
            connection=self._path,
            dialect="duckdb",
            max_rows=max_rows,
            max_distinct_for_enum=max_distinct_for_enum,
        )

    def dry_run(self, sql: str) -> DryRunResult:
        con = self._connect()
        try:
            con.execute(f"EXPLAIN {sql}")
            return DryRunResult(valid=True)
        except Exception as exc:
            return DryRunResult(valid=False, error=str(exc))
        finally:
            con.close()

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        con = self._connect()  # always read-only
        try:
            query = (
                sql
                if row_limit is None
                else f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {int(row_limit)}"
            )
            rel = con.execute(query)
            columns = [d[0] for d in rel.description] if rel.description else []
            rows = [list(r) for r in rel.fetchall()]
            return QueryResult(columns=columns, rows=rows)
        finally:
            con.close()

    def probe_write(self) -> None:
        """Prove write access: open the file writable, create a throwaway table, insert, drop."""
        import secrets

        table = f'"_dst_write_probe_{secrets.token_hex(4)}"'
        con = duckdb.connect(self._path, read_only=False)
        try:
            con.execute(f"CREATE TABLE {table} (probe INTEGER)")
            try:
                con.execute(f"INSERT INTO {table} VALUES (1)")
            finally:
                con.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            con.close()

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        """DuckDB keeps no history catalog — absence is a normal answer."""
        return []
