"""external_access — refuse SQL that reaches outside the warehouse's own tables.

A warehouse engine can do more than read tables: DuckDB reads any file or URL the
server process can reach (``read_csv``, ``read_text``, ``glob``, a quoted path in
FROM, ``ATTACH``, ``COPY``, ``INSTALL``/``LOAD``); Postgres reads server files and
runs SQL strings past any allow-list (``pg_read_file``, ``query_to_xml``,
``dblink``); Snowflake reads stages and other queries' results (``@stage``,
``TABLE(RESULT_SCAN(...))``); MySQL has ``LOAD_FILE``. None of that is a table a
lens modeled, and none of it is something a probe of a connection needs: the
admin who holds the credential reads the warehouse through it, not the server dst
runs on.

``refusal`` is the one check, applied whoever wrote the SQL: ``sql_guard.check``
runs it on every door (certified and connection-probe SQL included, where the
table allow-list is relaxed), and the DuckDB and Postgres connectors run it again
on everything they execute, so a path that reaches the warehouse without the
guard still cannot leave it. A local DuckDB file is additionally opened with
``enable_external_access`` off and the configuration locked (the engine-level
backstop); MotherDuck cannot be, because the MotherDuck extension loads at open,
so there this check is the boundary.

Every rule is by AST node, never by text: a FROM target that is not a table name
passes only when it generates rows from values already in the query (the same
class list ``sql_guard`` uses); on DuckDB a table name that looks like a path
(``"/etc/x.csv"``, ``'data.parquet'``) is refused because DuckDB would scan the
file; and a short list of scalar functions that reach files, the network, another
SQL string or the session configuration is refused by name.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

# Statement kinds that are never a read. sql_guard keeps its own list for its
# messages; this one is the backstop for paths that do not run the guard.
_NOT_A_READ = tuple(
    getattr(exp, n)
    for n in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "TruncateTable",
        "Command",
        "Set",
        "Pragma",
        "Copy",
        "Use",
        "Attach",
        "Detach",
        "Install",
        "Into",  # SELECT ... INTO (a table, or OUTFILE on MySQL)
    )
    if hasattr(exp, n)
)

# A FROM target that is a function passes only when it generates rows out of
# values already in the query. By sqlglot CLASS, never by function name: an
# unrecognized function parses to exp.Anonymous and stays refused.
ROW_GENERATORS = tuple(
    getattr(exp, n)
    for n in (
        "GenerateSeries",
        "ExplodingGenerateSeries",
        "GenerateDateArray",
        "GenerateTimestampArray",
        "Unnest",
        "Explode",
        "Posexplode",
        "Generator",
    )
    if hasattr(exp, n)
)
_DERIVED = (exp.Subquery, exp.Values) + ROW_GENERATORS

# Scalar functions refused by name, whatever the dialect (these names mean the
# same thing, or nothing, everywhere). Files and directories, large objects,
# other databases, SQL passed as a string (which no allow-list sees), and the
# session configuration (set_config can change the role).
DENIED_FUNCTIONS = frozenset(
    {
        # Postgres
        "pg_read_file",
        "pg_read_binary_file",
        "pg_stat_file",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_ls_tmpdir",
        "pg_ls_archive_statusdir",
        "pg_ls_logicalsnapdir",
        "pg_ls_logicalmapdir",
        "pg_ls_replslotdir",
        "pg_file_write",
        "pg_file_rename",
        "pg_file_unlink",
        "pg_logdir_ls",
        "lo_import",
        "lo_export",
        "lo_get",
        "lo_put",
        "lo_from_bytea",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_send_query",
        "dblink_get_result",
        "dblink_open",
        "dblink_fetch",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "table_to_xml",
        "table_to_xmlschema",
        "table_to_xml_and_xmlschema",
        "schema_to_xml",
        "schema_to_xmlschema",
        "schema_to_xml_and_xmlschema",
        "database_to_xml",
        "database_to_xmlschema",
        "database_to_xml_and_xmlschema",
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        # MySQL
        "load_file",
        # Snowflake: URLs to staged files
        "get_presigned_url",
        "build_scoped_file_url",
        "build_stage_file_url",
        "get_stage_location",
        "get_absolute_path",
        "get_relative_path",
    }
)

# On DuckDB a setting can hold a credential (the MotherDuck token, an S3 key).
# Postgres's current_setting reads session settings dst itself sets, so it stays.
_DUCKDB_DENIED = frozenset({"current_setting", "getenv"})

# DuckDB scans a file when a table name looks like one; a real table name never
# carries a path separator, a scheme, or a dot inside one identifier.
_PATH_CHARS = ("/", "\\", ":", ".")


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def _target_refusal(target: exp.Expr, dialect: str) -> str | None:
    """Why a FROM/JOIN target reaches outside the warehouse's tables, or None."""
    if isinstance(target, exp.Table):
        inner = target.this
        if isinstance(inner, exp.Identifier):
            if dialect == "duckdb" and any(c in inner.this for c in _PATH_CHARS):
                return f"'{inner.this}' names a file, not a table"
            return None
        return _target_refusal(inner, dialect)
    if isinstance(target, exp.Lateral | exp.TableFromRows):
        inner = target.this
        if isinstance(inner, exp.Func | exp.Dot | exp.Var):
            return _target_refusal(inner, dialect)
        return None
    if isinstance(target, _DERIVED):
        return None
    if isinstance(target, exp.Func):
        return f"table function '{_function_name(target)}' is not allowed"
    if isinstance(target, exp.Dot) and isinstance(target.expression, exp.Func):
        return f"table function '{_function_name(target.expression)}' is not allowed"
    if isinstance(target, exp.Var) and target.name.startswith("@"):
        return f"stage '{target.name}' is not a table"
    return f"unsupported FROM target ({type(target).__name__})"


def refusal_in(stmt: exp.Expr, dialect: str) -> str | None:
    """Why this parsed statement reaches outside the warehouse's tables, or None."""
    node = stmt.find(*_NOT_A_READ)
    if node is not None:
        return f"non-SELECT operation not allowed ({type(node).__name__})"
    for clause in stmt.find_all(exp.From, exp.Join):
        reason = _target_refusal(clause.this, dialect)
        if reason:
            return reason
    denied = DENIED_FUNCTIONS | _DUCKDB_DENIED if dialect == "duckdb" else DENIED_FUNCTIONS
    for func in stmt.find_all(exp.Func):
        if _function_name(func) in denied:
            return f"function '{_function_name(func)}' is not allowed"
    if dialect == "snowflake":
        for var in stmt.find_all(exp.Var):
            if var.name.startswith("@"):
                return f"stage '{var.name}' is not a table"
    return None


def refusal(sql: str, dialect: str) -> str | None:
    """Why this SQL may not run, or None: one parseable read statement that stays
    inside the warehouse's tables. SQL the parser cannot read is refused, since
    nothing about it can be shown to stay inside."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:
        return f"could not be parsed to check what it reads ({exc})"
    if len(statements) != 1:
        return "exactly one statement is allowed"
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        return "only SELECT queries are allowed"
    return refusal_in(stmt, dialect)


class ExternalAccessRefused(PermissionError):
    """Raised by a connector asked to run SQL that reaches outside the warehouse."""


def ensure_contained(sql: str, dialect: str) -> None:
    reason = refusal(sql, dialect)
    if reason:
        raise ExternalAccessRefused(f"SQL refused: {reason}")
