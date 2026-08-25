"""Snowflake connector — read-only, statement-timeout capped.

Auth is **keypair or password**. Keypair is the one with a future: Snowflake is
retiring single-factor password authentication, with final enforcement landing
August–October 2026, so a connector whose only credential is a stored password
stops working on their timetable rather than ours. `private_key` (PEM, optionally
passphrase-protected) is the supported replacement and needs no interactive step,
which is what makes it usable for a service that runs unattended.

Introspects through information_schema (tables + columns + row counts): the
declared ``schema`` when dst.yaml names one, otherwise EVERY schema in the
database — defaulting to PUBLIC makes a warehouse whose tables live elsewhere
introspect to nothing. The SQL-level SELECT-only guarantee is enforced
upstream by sql_guard.
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
from services.contracts.query_history import QueryRecord
from services.contracts.warehouse import (
    VALIDATION_SCHEMA,
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)


def _parse_clustering_key(raw: object) -> list[str]:
    """Parse Snowflake's CLUSTERING_KEY expression ("LINEAR(C1, C2)") into columns."""
    if not raw:
        return []
    inner = str(raw).strip()
    if "(" in inner and inner.endswith(")"):
        inner = inner[inner.index("(") + 1 : -1]
    return [c.strip() for c in inner.split(",") if c.strip()]


class SnowflakeConnector:
    kind = "snowflake"

    def __init__(
        self,
        *,
        account: str,
        user: str,
        password: str | None = None,
        private_key: str | None = None,
        private_key_passphrase: str | None = None,
        warehouse: str,
        database: str,
        schema: str | None = None,
        role: str | None = None,
        statement_timeout_ms: int = 30000,
    ) -> None:
        self._account = account
        self._user = user
        self._password = password
        self._private_key = private_key
        self._private_key_passphrase = private_key_passphrase
        self._warehouse = warehouse
        self._database = database
        self._schema = schema
        self._role = role
        self._statement_timeout_ms = statement_timeout_ms

    @classmethod
    def from_record(cls, config: dict[str, Any], secret: str | None) -> SnowflakeConnector:
        """Build from a stored connection record (config dict + decrypted secret).

        The secret is a password or a PEM private key; `auth` in the config says
        which, defaulting to keypair when the secret looks like PEM. Sniffing the
        shape rather than demanding the field means an operator who pastes a key
        into the existing credential box gets keypair auth instead of a confusing
        authentication failure.
        """
        if not secret:
            raise ValueError("snowflake connection has no stored credential")
        auth = str(config.get("auth") or "").lower()
        is_key = auth == "keypair" or (not auth and "-----BEGIN" in secret)
        return cls(
            account=str(config["account"]),
            user=str(config["user"]),
            password=None if is_key else secret,
            private_key=secret if is_key else None,
            private_key_passphrase=config.get("private_key_passphrase"),
            warehouse=str(config["warehouse"]),
            database=str(config["database"]),
            schema=str(config["schema"]) if config.get("schema") else None,
            role=config.get("role"),
        )

    @property
    def _label(self) -> str:
        """The connection's scope: database.schema when one is declared, else the
        whole database (which is what introspection now spans)."""
        return f"{self._database}.{self._schema}" if self._schema else self._database

    @property
    def _write_schema(self) -> str:
        """Where the write probe lands — reads span every schema, a write cannot."""
        return self._schema or "PUBLIC"

    def _schema_filter(self, column: str = "table_schema") -> tuple[str, tuple[str, ...]]:
        """WHERE fragment + params scoping an information_schema query."""
        if self._schema is not None:
            return f"{column} = %s", (self._schema,)
        return f"{column} NOT IN ('INFORMATION_SCHEMA', %s)", (VALIDATION_SCHEMA,)

    def _load_private_key(self) -> bytes:
        """PEM text -> DER bytes, which is the only form the connector accepts."""
        from cryptography.hazmat.primitives import serialization

        passphrase = self._private_key_passphrase
        key = serialization.load_pem_private_key(
            (self._private_key or "").encode(),
            password=passphrase.encode() if passphrase else None,
        )
        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def _connect(self) -> Any:
        import snowflake.connector

        from services.runtime import attribution

        auth: dict[str, Any] = (
            {"private_key": self._load_private_key()}
            if self._private_key
            else {"password": self._password}
        )
        session_params: dict[str, Any] = {
            "STATEMENT_TIMEOUT_IN_SECONDS": self._statement_timeout_ms // 1000
        }
        # Who ran this, in Snowflake's own QUERY_HISTORY.QUERY_TAG — a JSON blob
        # carrying principal, agent and the request_id that joins back to dst's
        # request_log. Self-asserted; see services/runtime/attribution.
        tag = attribution.query_tag()
        if tag is not None:
            session_params["QUERY_TAG"] = tag
        return snowflake.connector.connect(
            account=self._account,
            user=self._user,
            warehouse=self._warehouse,
            database=self._database,
            schema=self._schema,
            role=self._role,
            session_parameters=session_params,
            **auth,
        )

    def introspect(self) -> SchemaSnapshot:
        where, params = self._schema_filter()
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(
                    "SELECT table_schema, table_name, row_count, table_type "
                    f"FROM information_schema.tables WHERE {where}",
                    params,
                )
                row_counts: dict[str, int | None] = {}
                view_flags: dict[str, bool] = {}
                for schema, name, rc, table_type in cur.fetchall():
                    key = f"{schema}.{name}"
                    row_counts[key] = int(rc) if rc is not None else None
                    # The catalog's own word, carried so a view renders [VIEW]
                    # downstream instead of the fact dying at this connector.
                    view_flags[key] = str(table_type) != "BASE TABLE"
                cur.execute(
                    "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                    f"FROM information_schema.columns WHERE {where} "
                    "ORDER BY table_schema, table_name, ordinal_position",
                    params,
                )
                by_table: dict[str, list[ColumnSchema]] = {}
                for schema, table_name, column_name, data_type, is_nullable in cur.fetchall():
                    by_table.setdefault(f"{schema}.{table_name}", []).append(
                        ColumnSchema(
                            name=str(column_name),
                            type=str(data_type),
                            nullable=str(is_nullable) == "YES",
                        )
                    )
                schema_where, schema_params = self._schema_filter("schema_name")
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    f"WHERE {schema_where} ORDER BY 1",
                    schema_params,
                )
                searched = [str(s) for (s,) in cur.fetchall()]
                tables = [
                    TableSchema(
                        name=f"{self._database}.{table}",
                        columns=cols,
                        row_count=row_counts.get(table),
                        is_view=view_flags.get(table),
                    )
                    for table, cols in by_table.items()
                ]
                return SchemaSnapshot(
                    connection=self._label,
                    dialect="snowflake",
                    tables=tables,
                    schemas_searched=searched,
                )
            finally:
                cur.close()
        finally:
            con.close()

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile from INFORMATION_SCHEMA — no table scans.

        LAST_ALTERED is the best-effort physical freshness; ROW_COUNT,
        CLUSTERING_KEY and table/column comments come along for free.
        """
        where, params = self._schema_filter()
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(
                    "SELECT table_schema, table_name, row_count, last_altered, "
                    f"clustering_key, comment FROM information_schema.tables WHERE {where}",
                    params,
                )
                table_rows = cur.fetchall()
                cur.execute(
                    "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
                    f"comment FROM information_schema.columns WHERE {where} "
                    "ORDER BY table_schema, table_name, ordinal_position",
                    params,
                )
                col_rows = cur.fetchall()
            finally:
                cur.close()
        finally:
            con.close()
        columns: dict[str, list[ColumnProfile]] = {}
        for sname, tname, cname, ctype, nullable, comment in col_rows:
            columns.setdefault(f"{sname}.{tname}", []).append(
                ColumnProfile(
                    name=str(cname),
                    type=str(ctype),
                    nullable=str(nullable) == "YES",
                    description=str(comment) if comment else None,
                    description_source="warehouse" if comment else None,
                )
            )
        now = datetime.now(UTC)
        return [
            TableProfile(
                connection=self._label,
                # schema-qualified inside the database, exactly as introspect() names it
                table=f"{self._database}.{sname}.{tname}",
                description=str(comment) if comment else None,
                row_count=int(row_count) if row_count is not None else None,
                clustering=_parse_clustering_key(clustering_key),
                last_updated_physical=last_altered,
                profiled_at=now,
                source="catalog",
                columns=columns.get(f"{sname}.{tname}", []),
            )
            for sname, tname, row_count, last_altered, clustering_key, comment in table_rows
        ]

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """Snowflake doesn't enforce FKs and most schemas omit them; inference covers it."""
        return []

    # ── SamplingProfiler ──────────────────────────────────────────────────────

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Guarded sampling pass: ``SAMPLE (n ROWS)``-capped stats, read-only."""
        return sample_tables(
            self.execute,
            tables,
            connection=self._label,
            dialect="snowflake",
            max_rows=max_rows,
            max_distinct_for_enum=max_distinct_for_enum,
        )

    def dry_run(self, sql: str) -> DryRunResult:
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(f"EXPLAIN USING TEXT {sql}")
                return DryRunResult(valid=True)
            except Exception as exc:
                return DryRunResult(valid=False, error=str(exc))
            finally:
                cur.close()
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
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(query)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [list(r) for r in cur.fetchall()]
                return QueryResult(columns=columns, rows=rows)
            finally:
                cur.close()
        finally:
            con.close()

    def query_history(self, *, days: int = 30, limit: int = 1000) -> list[QueryRecord]:
        """Distinct SELECT statements from ACCOUNT_USAGE.QUERY_HISTORY.

        Scoped to this connection's database; statements + metadata only, never
        row data. ACCOUNT_USAGE lags ~45 min — fine for the probe's purposes.
        Requires IMPORTED PRIVILEGES on the SNOWFLAKE database (the audit grant).
        """
        sql = """
            SELECT
                q.query_text,
                MIN(q.start_time) AS first_seen,
                MAX(q.start_time) AS last_seen,
                COUNT(*) AS run_count,
                MAX(q.user_name) AS principal,
                MAX(s.client_application_id) AS source_tool
            FROM snowflake.account_usage.query_history q
            LEFT JOIN snowflake.account_usage.sessions s
              ON q.session_id = s.session_id
            WHERE q.query_type = 'SELECT'
              AND q.execution_status = 'SUCCESS'
              AND q.database_name = UPPER(%(database)s)
              AND q.start_time >= DATEADD('day', -%(days)s, CURRENT_TIMESTAMP())
              AND q.query_text NOT ILIKE '%%account_usage.query_history%%'
            GROUP BY q.query_text
            ORDER BY run_count DESC, last_seen DESC
            LIMIT %(limit)s
        """
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(sql, {"database": self._database, "days": days, "limit": limit})
                return [
                    QueryRecord(
                        statement=r[0],
                        first_seen=r[1],
                        last_seen=r[2],
                        run_count=int(r[3]),
                        principal=r[4],
                        source_tool=r[5],
                    )
                    for r in cur.fetchall()
                ]
            finally:
                cur.close()
        finally:
            con.close()

    def probe_write(self) -> None:
        """Prove write access: create a throwaway table in database.schema, insert, drop."""
        import secrets

        probe = secrets.token_hex(4).upper()
        table = f'{self._database}.{self._write_schema}."_DST_WRITE_PROBE_{probe}"'
        con = self._connect()
        try:
            cur = con.cursor()
            try:
                cur.execute(f"CREATE TABLE {table} (probe NUMBER)")
                try:
                    cur.execute(f"INSERT INTO {table} (probe) VALUES (1)")
                finally:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
            finally:
                cur.close()
        finally:
            con.close()
