"""DuckDB connector — the local, file-backed warehouse, and MotherDuck.

Read-only by construction: every QUERY connection is opened with ``read_only=True``,
so writes/DDL raise regardless of the ``read_only`` argument. The SQL-level SELECT-only
guarantee is additionally enforced by sql_guard. The single exception is
``probe_write``, which exists to prove write access and reopens the file writable —
it is reached only when a connection is registered asking for ``access: ["write"]``,
never from serving or from ``dst apply``.

A ``md:`` path is MotherDuck. The token rides the connection secret (``secret_env``),
never the path — a path is copied into snapshots, profiles and logs. MotherDuck
opens read-only under a regular token too (CREATE, INSERT, UPDATE and DELETE are
refused on the attached database), so the by-construction guarantee stays the
default there; a read-scaling token narrows the credential itself, and
``read_only: false`` opts out for a connection that must write. DuckDB caches one
configuration per ``md:`` database per process: a read-only open followed by a
read-write open of the same database in one process is refused, so ``probe_write``
on MotherDuck works only when nothing opened the database read-only first. Queries
on MotherDuck run on cursors of one connection the process holds per database,
opened under a deadline (``_motherduck_cursor``): an open that never returns fails
its request and is never waited on again. The first open in a process also loads
the MotherDuck extension, a download on a machine that never loaded it, so it has
its own longer bound (``DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S``), and ``warm`` lets an
apply pay it before a step deadline starts. A query the serving deadline gives up on
is interrupted, and one that still has not let go of that connection a few seconds
later retires it, so later queries never queue behind it. A MotherDuck session attaches
every database in the account, which is why every catalog read here is pinned to
``current_database()`` — a lens over one database must not see the others.

``statement_timeout_ms`` bounds a query the way ``statement_timeout`` does on
Postgres: a timer interrupts the connection, and the query fails naming the cap.
Unset means unbounded — right for a developer's own file, wrong for anything a
stranger can reach.

Introspection covers EVERY user schema, not the current one: ``SHOW TABLES``
(and a ``schema_name = 'main'`` filter in the catalog pass) makes a warehouse
whose tables live in any other schema look empty — a blank line and exit 0, the
worst failure shape there is. Names are qualified ``schema.table`` except in
``main``, which DuckDB resolves unqualified.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial

import duckdb

from services.config import settings
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
from services.runtime.bounded import Stalled, WarehouseTimeout, on_abandon, run_bounded

log = logging.getLogger("dst")

DEFAULT_SCHEMA = "main"

# Tables AND views (``SHOW TABLES`` listed both; jaffle's stg_* models are views),
# every user schema, no system relations — ``internal`` is the catalog's own flag
# for those, so nothing is matched by name.
#
# ``database_name = current_database()`` on every catalog read: an attached
# database (every MotherDuck database in the account, or a local ATTACH) is not
# this connection's warehouse.
_THIS_DB = "database_name = current_database()"
_RELATIONS = (
    "SELECT schema_name, table_name, comment, FALSE AS is_view FROM duckdb_tables() "
    f"WHERE NOT internal AND {_THIS_DB} "
    "UNION ALL "
    "SELECT schema_name, view_name, comment, TRUE FROM duckdb_views() "
    f"WHERE NOT internal AND {_THIS_DB} "
    "ORDER BY 1, 2"
)
_COLUMNS = (
    "SELECT schema_name, table_name, column_name, data_type, is_nullable, comment "
    f"FROM duckdb_columns() WHERE NOT internal AND {_THIS_DB} "
    "ORDER BY schema_name, table_name, column_index"
)
# The catalog pass sees exactly what introspect() lists — views included. Reading
# duckdb_tables() alone made every relation a view unprofilable, and every dbt
# staging model is a view: they were listed for authoring and could never carry a
# profile fact. Views have no estimated_size — introspect()'s own count(*) is
# what puts a row count next to them in the listing.
_CATALOG_RELATIONS = (
    "SELECT schema_name, table_name, estimated_size, comment, FALSE AS is_view "
    f"FROM duckdb_tables() WHERE NOT internal AND {_THIS_DB} "
    "UNION ALL "
    "SELECT schema_name, view_name, NULL::BIGINT, comment, TRUE FROM duckdb_views() "
    f"WHERE NOT internal AND {_THIS_DB} "
    "ORDER BY 1, 2"
)
# `main` is flagged internal in duckdb_schemas(), so the user's schemas are
# "everything in THIS database, minus dst's own validation plane".
_SCHEMAS = (
    "SELECT DISTINCT schema_name FROM duckdb_schemas() "
    f"WHERE {_THIS_DB} AND schema_name != ? ORDER BY 1"
)

MOTHERDUCK_PREFIX = "md:"

# How long opening a MotherDuck connection, or a cursor on one, may take before it
# counts as wedged rather than slow: a cold attach takes about a second. The same
# bound as one BigQuery API round-trip. The first open in a process is bounded by
# DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S instead (``_Root.open_timeout``).
OPEN_TIMEOUT_S = 60.0

# How long an interrupted statement may take to let go of its connection before
# the connection counts as occupied for good.
INTERRUPT_GRACE_S = 5.0


def is_motherduck(path: str) -> bool:
    return path.startswith(MOTHERDUCK_PREFIX)


class _Root:
    """The process's one MotherDuck connection for a (path, access mode, token)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.generation = 0
        self.con: duckdb.DuckDBPyConnection | None = None
        self.lock = threading.Lock()
        self.opened = False  # an open of this database has succeeded in this process

    def open_timeout(self) -> float | None:
        """The bound on opening: the first open in the process also loads the
        MotherDuck extension (downloading it on a machine that never loaded it)
        and attaches for the first time, so it gets its own longer bound
        (``DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S``; None when 0 disables it)."""
        if self.opened:
            return OPEN_TIMEOUT_S
        first = settings.warehouse_first_open_timeout_s
        return first if first > 0 else None

    @property
    def key(self) -> str:
        """The connection string DuckDB caches the instance under. A retired
        instance's successor gets a new one: an extra ``user`` parameter is
        MotherDuck's documented way to get a separate instance."""
        if not self.generation:
            return self.path
        return f"{self.path}{'&' if '?' in self.path else '?'}user=dst-{self.generation}"

    def retire(self) -> None:
        """Never touch this instance again; the next call opens a fresh one."""
        self.con = None
        self.generation += 1


_roots: dict[tuple[str, bool, str | None], _Root] = {}
_roots_lock = threading.Lock()


def _motherduck_open(
    path: str, read_only: bool, token: str | None
) -> tuple[_Root, duckdb.DuckDBPyConnection]:
    """The process's MotherDuck connection for *path* (see ``_motherduck_cursor``),
    opened here on first use under the root's open bound; the open's seconds go to
    the log, so a slow one is measurable from the server log alone."""
    with _roots_lock:
        root = _roots.setdefault((path, read_only, token), _Root(path))
    step = f"opening MotherDuck database '{path}'"
    first = not root.opened
    bound = root.open_timeout()
    setting = "DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S" if first else None
    if not root.lock.acquire(timeout=-1 if bound is None else bound):
        raise WarehouseTimeout(step, bound or 0, setting)
    try:
        con = root.con
        if con is None:
            config: dict[str, str | bool | int | float | list[str]] = (
                {"motherduck_token": token} if token else {}
            )
            opener = partial(duckdb.connect, root.key, read_only=read_only, config=config)
            started = time.perf_counter()
            try:
                con = (
                    opener() if bound is None else run_bounded("dst-motherduck-open", opener, bound)
                )
            except Stalled:
                log.error("%s did not return within %gs; retiring that instance", step, bound)
                root.retire()
                raise WarehouseTimeout(step, bound or 0, setting) from None
            root.con, root.opened = con, True
            log.info(
                "opened MotherDuck database '%s' in %.2fs%s",
                path,
                time.perf_counter() - started,
                " (its first open in this process)" if first else "",
            )
    finally:
        root.lock.release()
    return root, con


def _motherduck_cursor(
    path: str, read_only: bool, token: str | None
) -> tuple[duckdb.DuckDBPyConnection, Callable[[], None]]:
    """A cursor on the process's MotherDuck connection for *path*, opened on first
    use, and the call that retires that connection.

    DuckDB's Python client caches one database instance per connection string,
    process-wide, and an open of a string whose instance is being created or torn
    down waits for it inside DuckDB. A MotherDuck handshake that never completed
    left every later open of that database spinning in that wait for as long as
    the process lived; opening per call (two or three opens per served question,
    and a teardown after every idle spell) kept the window wide open. So: one
    connection per (path, mode, token), held for the life of the process, and a
    cursor per call — a cursor never touches the cache. Each cursor, and every
    open after the first, runs under ``OPEN_TIMEOUT_S``; the first open in the
    process runs under its own longer bound (``_Root.open_timeout``). An open that
    stalls retires the instance, and the next call opens a fresh one under a new
    connection string instead of waiting on the wedged one. A call queued behind
    an open waits here, bounded, never inside DuckDB. The stalled thread cannot be
    killed; it is a daemon."""
    root, con = _motherduck_open(path, read_only, token)
    step = f"opening MotherDuck database '{path}'"

    def retire() -> None:
        with root.lock:
            if root.con is con:
                root.retire()

    try:
        return run_bounded("dst-motherduck-cursor", con.cursor, OPEN_TIMEOUT_S), retire
    except Stalled:
        retire()
        raise WarehouseTimeout(step, OPEN_TIMEOUT_S) from None


def _let_go(
    con: duckdb.DuckDBPyConnection, freed: threading.Event, retire: Callable[[], None] | None
) -> None:
    """The deadline a call runs under gave up on it: interrupt its statement. On
    MotherDuck, a statement that has not let go within ``INTERRUPT_GRACE_S`` still
    occupies the one connection the process holds, and every later cursor could
    queue behind it, so that connection is retired and the next call opens a fresh
    instance."""
    if freed.is_set():
        return
    con.interrupt()
    if retire is not None and not freed.wait(INTERRUPT_GRACE_S):
        log.warning(
            "a MotherDuck statement past its deadline did not stop when interrupted; "
            "retiring the connection it holds"
        )
        retire()


def qualified(schema: str, table: str) -> str:
    """``schema.table``, except in ``main`` — DuckDB resolves an unqualified name
    against ``main`` only, so its tables stay copy-pasteable into SQL and a lens
    can keep addressing ``customers`` unqualified."""
    return table if schema == DEFAULT_SCHEMA else f"{schema}.{table}"


class DuckDBConnector:
    kind = "duckdb"

    def __init__(
        self,
        path: str,
        *,
        profile: bool = True,
        schema: str | None = None,
        token: str | None = None,
        read_only: bool = True,
        statement_timeout_ms: int | None = None,
    ) -> None:
        if "motherduck_token=" in path:
            raise ValueError(
                "the MotherDuck token belongs in the connection's secret_env, not in the "
                "path — the path is copied into snapshots, profiles and logs"
            )
        self._path = path
        self._profile = profile
        self._schema = schema  # None = every user schema
        self._token = token
        # Only MotherDuck can open read-write on the query path; a local file
        # is read-only by construction whatever the flag says.
        self._read_only = read_only or not is_motherduck(path)
        self._timeout_ms = statement_timeout_ms if statement_timeout_ms else None

    @property
    def statement_timeout_ms(self) -> int | None:
        return self._timeout_ms

    def _md_config(self) -> dict[str, str | bool | int | float | list[str]]:
        return {"motherduck_token": self._token} if self._token else {}

    def warm(self) -> None:
        """Pay this warehouse's one-time cost in this process, so a deadline on
        a later step measures the warehouse: on MotherDuck, the first open, which
        loads the extension (downloading it on a machine that never loaded it)
        and attaches. A no-op for a local file and once the database is open."""
        if is_motherduck(self._path):
            _motherduck_open(self._path, self._read_only, self._token)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """A connection to the local file for one call; the caller closes it."""
        return duckdb.connect(self._path, read_only=True)

    @contextmanager
    def _session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A connection for one call, closed after it. On MotherDuck it is a cursor
        on the process's connection (see ``_motherduck_cursor``). If the deadline the
        call runs under gives up on it, its statement is interrupted (``_let_go``)."""
        retire: Callable[[], None] | None = None
        if is_motherduck(self._path):
            con, retire = _motherduck_cursor(self._path, self._read_only, self._token)
        else:
            con = self._connect()
        freed = threading.Event()
        on_abandon(partial(_let_go, con, freed, retire))
        try:
            yield con
        finally:
            try:
                con.close()
            finally:
                freed.set()

    def _run(self, con: duckdb.DuckDBPyConnection, query: str) -> duckdb.DuckDBPyConnection:
        """``con.execute`` under the statement timeout: a timer interrupts the
        connection, and the failure names the cap instead of a bare interrupt."""
        if self._timeout_ms is None:
            return con.execute(query)
        timer = threading.Timer(self._timeout_ms / 1000, con.interrupt)
        timer.start()
        try:
            return con.execute(query)
        except duckdb.InterruptException as exc:
            raise duckdb.Error(
                f"query cancelled after {self._timeout_ms} ms "
                "(this connection's statement_timeout_ms)"
            ) from exc
        finally:
            timer.cancel()

    def _searched_schemas(self, con: duckdb.DuckDBPyConnection) -> list[str]:
        if self._schema:
            return [self._schema]
        return [str(s) for (s,) in con.execute(_SCHEMAS, [VALIDATION_SCHEMA]).fetchall()]

    def _in_scope(self, schema: str) -> bool:
        return schema != VALIDATION_SCHEMA and (self._schema is None or schema == self._schema)

    def introspect(self) -> SchemaSnapshot:
        with self._session() as con:
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

    # ── CatalogProfiler ───────────────────────────────────────────────────────

    def profile_catalog(self) -> list[TableProfile]:
        """Catalog-only profile from duckdb_tables()/duckdb_views()/duckdb_columns() —
        no table scans.

        Row counts are the catalog's estimates (views have none); the database
        file's mtime is the best-effort physical freshness.
        """
        modified = self._file_modified()
        with self._session() as con:
            table_rows = con.execute(_CATALOG_RELATIONS).fetchall()
            col_rows = con.execute(_COLUMNS).fetchall()
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
        with self._session() as con:
            try:
                self._run(con, f"EXPLAIN {sql}")
                return DryRunResult(valid=True)
            except Exception as exc:
                return DryRunResult(valid=False, error=str(exc))

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        with self._session() as con:  # always read-only
            query = (
                sql
                if row_limit is None
                else f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {int(row_limit)}"
            )
            query = tag_sql(query)  # the final statement identifies itself in query history
            rel = self._run(con, query)
            columns = [d[0] for d in rel.description] if rel.description else []
            rows = [list(r) for r in rel.fetchall()]
            return QueryResult(columns=columns, rows=rows)

    def probe_write(self) -> None:
        """Prove write access: open the file writable, create a throwaway table, insert, drop."""
        import secrets

        table = f'"_dst_write_probe_{secrets.token_hex(4)}"'
        con = duckdb.connect(self._path, read_only=False, config=self._md_config())
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
