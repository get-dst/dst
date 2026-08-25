"""MySQL connector — read-only, statement-timeout-capped (MAX_EXECUTION_TIME).

Auth via host/port/database/user + password. Introspects a database (MySQL's
``schema`` == ``database``) through ``information_schema`` (tables + columns +
row-count estimates). The PyMySQL driver is imported lazily so this module stays
importable when the driver isn't installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.connectors.sampling import sample_tables
from services.connectors.tagging import tag_sql
from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    ColumnProfile,
    JoinCandidate,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.warehouse import (
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)


class MySQLConnector:
    kind = "mysql"

    def __init__(
        self,
        *,
        host: str,
        port: int = 3306,
        database: str,
        user: str,
        password: str,
        statement_timeout_ms: int = 30000,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._statement_timeout_ms = statement_timeout_ms

    @classmethod
    def from_record(cls, config: dict[str, Any], password: str | None) -> MySQLConnector:
        """Build from a stored connection record (config dict + decrypted secret)."""
        return cls(
            host=str(config["host"]),
            port=int(config.get("port", 3306)),
            database=str(config["database"]),
            user=str(config["user"]),
            password=password or "",
            statement_timeout_ms=int(config.get("statement_timeout_ms", 30000)),
        )

    def _connect(self) -> Any:
        import pymysql

        return pymysql.connect(
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=self._password,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def introspect(self) -> SchemaSnapshot:
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT table_name AS table_name, table_rows AS table_rows "
                    "FROM information_schema.tables WHERE table_schema = %s",
                    (self._database,),
                )
                row_counts = {r["table_name"]: r["table_rows"] for r in cur.fetchall()}
                cur.execute(
                    "SELECT table_name AS table_name, column_name AS column_name, "
                    "data_type AS data_type, is_nullable AS is_nullable "
                    "FROM information_schema.columns WHERE table_schema = %s "
                    "ORDER BY table_name, ordinal_position",
                    (self._database,),
                )
                by_table: dict[str, list[ColumnSchema]] = {}
                for r in cur.fetchall():
                    by_table.setdefault(r["table_name"], []).append(
                        ColumnSchema(
                            name=r["column_name"],
                            type=r["data_type"],
                            nullable=r["is_nullable"] == "YES",
                        )
                    )
            tables = [
                TableSchema(
                    name=tname,
                    columns=cols,
                    row_count=(
                        int(row_counts[tname]) if row_counts.get(tname) is not None else None
                    ),
                )
                for tname, cols in by_table.items()
            ]
            # In MySQL a schema IS a database, and `database` is required config —
            # so there is no default-schema blind spot to fix here, only the
            # searched scope to name when nothing comes back.
            return SchemaSnapshot(
                connection=self._database,
                dialect="mysql",
                tables=tables,
                schemas_searched=[self._database],
            )
        finally:
            con.close()

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile from information_schema — no table scans.

        UPDATE_TIME is the best-effort physical freshness (NULL on many
        configs/engines); row counts are estimates; table/column comments come
        along for free.
        """
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT table_name AS table_name, table_rows AS table_rows, "
                    "update_time AS update_time, table_comment AS table_comment "
                    "FROM information_schema.tables WHERE table_schema = %s",
                    (self._database,),
                )
                table_rows = cur.fetchall()
                cur.execute(
                    "SELECT table_name AS table_name, column_name AS column_name, "
                    "data_type AS data_type, is_nullable AS is_nullable, "
                    "column_comment AS column_comment "
                    "FROM information_schema.columns WHERE table_schema = %s "
                    "ORDER BY table_name, ordinal_position",
                    (self._database,),
                )
                col_rows = cur.fetchall()
        finally:
            con.close()
        columns: dict[str, list[ColumnProfile]] = {}
        for r in col_rows:
            comment = r["column_comment"] or None
            columns.setdefault(r["table_name"], []).append(
                ColumnProfile(
                    name=r["column_name"],
                    type=r["data_type"],
                    nullable=r["is_nullable"] == "YES",
                    description=comment,
                    description_source="warehouse" if comment else None,
                )
            )
        now = datetime.now(UTC)
        return [
            TableProfile(
                connection=self._database,
                table=r["table_name"],
                description=r["table_comment"] or None,
                row_count=int(r["table_rows"]) if r["table_rows"] is not None else None,
                last_updated_physical=r["update_time"],
                profiled_at=now,
                source="catalog",
                columns=columns.get(r["table_name"], []),
            )
            for r in table_rows
        ]

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """Declared FKs from KEY_COLUMN_USAGE → join candidates with evidence="fk"."""
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT constraint_name AS constraint_name, table_name AS table_name, "
                    "column_name AS column_name, "
                    "referenced_table_name AS referenced_table_name, "
                    "referenced_column_name AS referenced_column_name "
                    "FROM information_schema.key_column_usage "
                    "WHERE table_schema = %s AND referenced_table_name IS NOT NULL "
                    "ORDER BY constraint_name, ordinal_position",
                    (self._database,),
                )
                rows = cur.fetchall()
        finally:
            con.close()
        # group by constraint so multi-column FKs stay one candidate
        grouped: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}
        for r in rows:
            key = (r["constraint_name"], r["table_name"], r["referenced_table_name"])
            left, right = grouped.setdefault(key, ([], []))
            left.append(r["column_name"])
            right.append(r["referenced_column_name"])
        return [
            JoinCandidate(
                left_table=table,
                left_columns=left,
                right_table=referenced,
                right_columns=right,
                evidence="fk",
            )
            for (_, table, referenced), (left, right) in grouped.items()
        ]

    # ── SamplingProfiler ──────────────────────────────────────────────────────

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Guarded sampling pass: MySQL has no TABLESAMPLE, so a plain ``LIMIT``
        is the (unordered) sample; the same row cap and shape-only rule apply."""
        return sample_tables(
            self.execute,
            tables,
            connection=self._database,
            dialect="mysql",
            max_rows=max_rows,
            max_distinct_for_enum=max_distinct_for_enum,
        )

    def dry_run(self, sql: str) -> DryRunResult:
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(f"EXPLAIN {sql}")
            return DryRunResult(valid=True)
        except Exception as exc:
            return DryRunResult(valid=False, error=str(exc))
        finally:
            con.close()

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        query = (
            sql
            if row_limit is None
            else f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {int(row_limit)}"
        )
        query = tag_sql(query)  # the final statement identifies itself in query history
        con = self._connect()  # autocommit read-only session
        try:
            with con.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(self._statement_timeout_ms)}")
                cur.execute(query)
                desc = cur.description
                columns = [d[0] for d in desc] if desc else []
                rows = [[r[c] for c in columns] for r in cur.fetchall()]
            return QueryResult(columns=columns, rows=rows)
        finally:
            con.close()

    def probe_write(self) -> None:
        """Prove write access: create a throwaway table in the database, insert, drop."""
        import secrets

        table = f"`{self._database}`.`_dst_write_probe_{secrets.token_hex(4)}`"
        con = self._connect()  # autocommit; not the read-only execute() session
        try:
            with con.cursor() as cur:
                cur.execute(f"CREATE TABLE {table} (probe INT)")
                try:
                    cur.execute(f"INSERT INTO {table} (probe) VALUES (1)")
                finally:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            con.close()
