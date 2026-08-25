"""PostgreSQL connector — read-only, statement-timeout capped.

Auth via standard libpq params (host/port/database/user/password/sslmode).
Introspects through ``information_schema`` (tables + columns): the declared
``schema`` when dst.yaml names one, otherwise EVERY non-system schema —
defaulting to "public" makes a warehouse whose tables live anywhere else look
empty, the same silent-nothing shape DuckDB has when it lists only ``main``.
Table names are always schema-qualified. Every connection is opened read-only,
so writes/DDL raise
regardless of the ``read_only`` argument. The SQL-level SELECT-only guarantee is
additionally enforced by sql_guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.connectors.sampling import sample_tables
from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    ColumnProfile,
    JoinCandidate,
    PartitioningProfile,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.warehouse import (
    VALIDATION_SCHEMA,
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)

_PARTITION_STRATEGY = {"r": "range", "l": "list", "h": "hash"}


def _distinct_count(n_distinct: float | None, row_estimate: int | None) -> int | None:
    """Resolve pg_stats.n_distinct: >0 is absolute, <0 is a fraction of rows, 0 unknown."""
    if n_distinct is None or n_distinct == 0:
        return None
    if n_distinct > 0:
        return int(n_distinct)
    if row_estimate is not None and row_estimate >= 0:
        return int(round(-n_distinct * row_estimate))
    return None


class PostgresConnector:
    kind = "postgres"

    def __init__(
        self,
        *,
        host: str,
        port: int = 5432,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
        schema: str | None = None,
        statement_timeout_ms: int = 30000,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._sslmode = sslmode
        self._schema = schema  # None = every non-system schema
        self._statement_timeout_ms = statement_timeout_ms

    @classmethod
    def from_record(cls, config: dict[str, Any], password: str | None) -> PostgresConnector:
        """Build from a stored connection record (config jsonb + decrypted secret)."""
        declared = config.get("schema")
        return cls(
            host=str(config["host"]),
            port=int(config.get("port", 5432)),
            database=str(config["database"]),
            user=str(config["user"]),
            password=password or "",
            sslmode=str(config.get("sslmode", "prefer")),
            schema=str(declared) if declared else None,
            statement_timeout_ms=int(config.get("statement_timeout_ms", 30000)),
        )

    @property
    def _write_schema(self) -> str:
        """Where the write probe and any single-schema write lands — "public"
        unless a schema is declared (reads span all of them; a write cannot)."""
        return self._schema or "public"

    def _schema_filter(self, column: str) -> tuple[str, tuple[Any, ...]]:
        """WHERE fragment + params scoping a catalog query to this connection's
        schemas. ``!~ '^pg_'`` covers pg_catalog/pg_toast/pg_temp_N without
        LIKE-escape games; dst's own validation plane is never data."""
        if self._schema is not None:
            return f"{column} = %s", (self._schema,)
        return f"{column} !~ '^pg_' AND {column} NOT IN ('information_schema', %s)", (
            VALIDATION_SCHEMA,
        )

    def _connect(self, *, writable: bool = False) -> Any:
        import psycopg

        from services.runtime import attribution

        options = f"-c statement_timeout={int(self._statement_timeout_ms)}"
        if not writable:
            options += " -c default_transaction_read_only=on"
        return psycopg.connect(
            host=self._host,
            port=self._port,
            dbname=self._database,
            user=self._user,
            password=self._password,
            sslmode=self._sslmode,
            autocommit=True,
            options=options,
            # Who ran this, in the warehouse's own pg_stat_activity — carries the
            # request_id that joins back to dst's request_log. Self-asserted; see
            # services/runtime/attribution.
            application_name=attribution.application_name(),
        )

    def introspect(self) -> SchemaSnapshot:
        where, params = self._schema_filter("table_schema")
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                f"FROM information_schema.columns WHERE {where} "
                "ORDER BY table_schema, table_name, ordinal_position",
                params,
            )
            by_table: dict[str, list[ColumnSchema]] = {}
            for schema_name, table_name, column_name, data_type, is_nullable in cur.fetchall():
                by_table.setdefault(f"{schema_name}.{table_name}", []).append(
                    ColumnSchema(
                        name=column_name,
                        type=data_type,
                        nullable=is_nullable == "YES",
                    )
                )
            tables = [TableSchema(name=name, columns=cols) for name, cols in by_table.items()]
            ns_where, ns_params = self._schema_filter("nspname")
            searched = con.execute(
                f"SELECT nspname FROM pg_catalog.pg_namespace WHERE {ns_where} ORDER BY 1",
                ns_params,
            ).fetchall()
            return SchemaSnapshot(
                connection=self._database,
                dialect="postgres",
                tables=tables,
                schemas_searched=[str(s) for (s,) in searched],
            )
        finally:
            con.close()

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile — reads pg_catalog/information_schema, never the tables.

        ``pg_stats`` provides free column profiling (null_frac, n_distinct,
        most_common_vals) collected by ANALYZE; declared partitioning comes from
        ``pg_partitioned_table``; ``last_analyze``/``last_autoanalyze`` stand in
        for physical freshness (best-effort — they track ANALYZE, not writes).
        """
        cols_where, cols_params = self._schema_filter("table_schema")
        ns_where, ns_params = self._schema_filter("n.nspname")
        stats_where, stats_params = self._schema_filter("schemaname")
        con = self._connect()
        try:
            # names/types — the same view introspect() reads
            cur = con.execute(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                f"FROM information_schema.columns WHERE {cols_where} "
                "ORDER BY table_schema, table_name, ordinal_position",
                cols_params,
            )
            column_rows = cur.fetchall()
            # table comments, row estimates, ANALYZE timestamps
            cur = con.execute(
                "SELECT n.nspname, c.relname, "
                "       obj_description(c.oid, 'pg_class'), "
                "       c.reltuples::bigint, "
                "       GREATEST(s.last_analyze, s.last_autoanalyze), "
                "       c.relkind IN ('v', 'm') AS is_view "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid "
                # 'v' too: introspect() reads information_schema.columns, which lists
                # views — without them the catalog pass cannot profile a relation the
                # listing offers for authoring, and every dbt staging model is a view.
                f"WHERE {ns_where} AND c.relkind IN ('r', 'p', 'm', 'v')",
                ns_params,
            )
            table_rows = cur.fetchall()
            # column comments
            cur = con.execute(
                "SELECT n.nspname, c.relname, a.attname, col_description(c.oid, a.attnum) "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_catalog.pg_attribute a "
                "  ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                f"WHERE {ns_where} AND col_description(c.oid, a.attnum) IS NOT NULL",
                ns_params,
            )
            comment_rows = cur.fetchall()
            # pg_stats freebies: null fraction, distinct estimate, common values
            cur = con.execute(
                "SELECT schemaname, tablename, attname, null_frac, n_distinct, "
                "most_common_vals::text::text[] "
                f"FROM pg_catalog.pg_stats WHERE {stats_where}",
                stats_params,
            )
            stats_rows = cur.fetchall()
            # declared partitioning
            cur = con.execute(
                "SELECT n.nspname, c.relname, pt.partstrat::text, a.attname "
                "FROM pg_catalog.pg_partitioned_table pt "
                "JOIN pg_catalog.pg_class c ON c.oid = pt.partrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_catalog.pg_attribute a "
                "  ON a.attrelid = c.oid AND a.attnum = pt.partattrs[0] "
                f"WHERE {ns_where}",
                ns_params,
            )
            partition_rows = cur.fetchall()
        finally:
            con.close()

        facts: dict[str, tuple[str | None, int | None, datetime | None, bool]] = {}
        for sname, tname, comment, reltuples, analyzed, is_view in table_rows:
            rows = int(reltuples) if reltuples is not None and reltuples >= 0 else None
            facts[f"{sname}.{tname}"] = (comment, rows, analyzed, bool(is_view))
        comments = {(f"{s}.{t}", str(c)): d for s, t, c, d in comment_rows}
        stats = {(f"{s}.{t}", str(c)): (nf, nd, mcv) for s, t, c, nf, nd, mcv in stats_rows}
        partitioning = {
            f"{s}.{t}": PartitioningProfile(
                column=str(col) if col else None, kind=_PARTITION_STRATEGY.get(str(strat))
            )
            for s, t, strat, col in partition_rows
        }

        columns: dict[str, list[ColumnProfile]] = {}
        for sname, tname, cname, dtype, nullable in column_rows:
            table, column = f"{sname}.{tname}", str(cname)
            _, row_estimate, _, _ = facts.get(table, (None, None, None, False))
            null_frac, n_distinct, mcv = stats.get((table, column), (None, None, None))
            distinct = _distinct_count(n_distinct, row_estimate)
            low_cardinality = distinct is not None and 0 < distinct <= LOW_CARDINALITY_MAX
            description = comments.get((table, column))
            columns.setdefault(table, []).append(
                ColumnProfile(
                    name=column,
                    type=str(dtype),
                    nullable=str(nullable) == "YES",
                    description=description,
                    description_source="warehouse" if description else None,
                    null_rate=float(null_frac) if null_frac is not None else None,
                    distinct_count=distinct,
                    is_low_cardinality=low_cardinality,
                    top_values=(
                        [str(v) for v in mcv[:LOW_CARDINALITY_MAX]]
                        if low_cardinality and mcv
                        else None
                    ),
                )
            )

        now = datetime.now(UTC)
        profiles: list[TableProfile] = []
        for table, cols in columns.items():
            comment, row_estimate, analyzed, is_view = facts.get(table, (None, None, None, False))
            profiles.append(
                TableProfile(
                    connection=self._database,
                    table=table,  # already schema-qualified, exactly as introspect() names it
                    description=comment,
                    row_count=row_estimate,
                    is_view=is_view,
                    partitioning=partitioning.get(table),
                    last_updated_physical=analyzed,
                    profiled_at=now,
                    source="catalog",
                    columns=cols,
                )
            )
        return profiles

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """Declared foreign keys (pg_constraint) → join candidates with evidence="fk"."""
        where, params = self._schema_filter("n.nspname")
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT n.nspname, cl.relname, pn.nspname, pc.relname, "
                "       (SELECT array_agg(a.attname ORDER BY k.ord) "
                "          FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
                "          JOIN pg_catalog.pg_attribute a "
                "            ON a.attrelid = con.conrelid AND a.attnum = k.attnum), "
                "       (SELECT array_agg(a.attname ORDER BY k.ord) "
                "          FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord) "
                "          JOIN pg_catalog.pg_attribute a "
                "            ON a.attrelid = con.confrelid AND a.attnum = k.attnum) "
                "FROM pg_catalog.pg_constraint con "
                "JOIN pg_catalog.pg_class cl ON cl.oid = con.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace "
                "JOIN pg_catalog.pg_class pc ON pc.oid = con.confrelid "
                "JOIN pg_catalog.pg_namespace pn ON pn.oid = pc.relnamespace "
                f"WHERE con.contype = 'f' AND {where} "
                "ORDER BY cl.relname, pc.relname",
                params,
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [
            JoinCandidate(
                left_table=f"{cns}.{child}",
                left_columns=[str(c) for c in lcols],
                right_table=f"{pns}.{parent}",
                right_columns=[str(c) for c in rcols],
                evidence="fk",
            )
            for cns, child, pns, parent, lcols, rcols in rows
        ]

    # ── SamplingProfiler ──────────────────────────────────────────────────────

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Guarded sampling pass: ``TABLESAMPLE SYSTEM`` + LIMIT, read-only.

        The orchestrator only requests columns the catalog pass couldn't already
        profile — anything ``pg_stats`` covered (after ANALYZE) is never sampled.
        """
        return sample_tables(
            self.execute,
            tables,
            connection=self._database,
            dialect="postgres",
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
            cur = con.execute(query)
            columns = [d.name for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
            return QueryResult(columns=columns, rows=rows)
        finally:
            con.close()

    def probe_write(self) -> None:
        """Prove write access: create a throwaway table in the schema, insert, drop."""
        import secrets

        table = f'"{self._write_schema}"."_dst_write_probe_{secrets.token_hex(4)}"'
        con = self._connect(writable=True)
        try:
            con.execute(f"CREATE TABLE {table} (probe integer)")
            try:
                con.execute(f"INSERT INTO {table} (probe) VALUES (1)")
            finally:
                con.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            con.close()
