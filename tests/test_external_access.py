"""SQL that reaches past the warehouse's tables is refused on every door.

DuckDB reads any file or URL the server process can reach, and Postgres, MySQL and
Snowflake each have their own ways out (server files, SQL passed as a string,
stages). What these tests pin: every such vector is refused by the guard whoever
wrote the SQL (connection probe, lens probe, certified SQL), by the connector
whatever path sent it, and on a local DuckDB file by the engine itself; and the
reads people actually do still work.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.connectors.mysql import MySQLConnector
from services.connectors.postgres import PostgresConnector
from services.contracts.lens_config import AccessRule
from services.contracts.semantic_model import SemanticModel
from services.lenses.demo import jaffle_customer_value_bundle
from services.runtime import external_access, sql_guard
from services.runtime.external_access import ExternalAccessRefused

SECRET = "dst-external-access-canary"


@pytest.fixture
def secret_files(tmp_path: Path) -> Path:
    """A directory of files no statement may read, each holding SECRET."""
    (tmp_path / "s.json").write_text(f'{{"k": "{SECRET}"}}')
    (tmp_path / "s.csv").write_text(f"k\n{SECRET}\n")
    (tmp_path / "s.txt").write_text(SECRET)
    with duckdb.connect(str(tmp_path / "other.duckdb")) as c:
        c.execute(f"CREATE TABLE t AS SELECT '{SECRET}' AS k")
    return tmp_path


def duckdb_vectors(d: Path) -> list[str]:
    return [
        f"SELECT * FROM read_json_auto('{d}/s.json')",
        f"SELECT * FROM read_csv('{d}/s.csv')",
        f"SELECT * FROM read_csv_auto('{d}/s.csv')",
        f"SELECT content FROM read_text('{d}/s.txt')",
        f"SELECT content FROM read_blob('{d}/s.txt')",
        f"SELECT * FROM read_parquet('{d}/x.parquet')",
        f"SELECT * FROM glob('{d}/*')",
        f"SELECT * FROM '{d}/s.csv'",
        f'SELECT * FROM "{d}/s.csv"',
        "SELECT * FROM 's.csv'",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
        f"SELECT k FROM (SELECT * FROM read_text('{d}/s.txt')) AS q(k)",
        f"WITH a AS (SELECT content FROM read_text('{d}/s.txt')) SELECT content FROM a",
        f"SELECT 1 AS n WHERE EXISTS (SELECT 1 FROM read_text('{d}/s.txt'))",
        # a LATERAL table function was waved through as a derived relation
        f"SELECT r.content FROM customers AS c, LATERAL read_text('{d}/s.txt') AS r",
        f"SELECT r.content FROM customers AS c CROSS JOIN read_text('{d}/s.txt') AS r",
        f"SELECT * FROM query('SELECT * FROM read_text(''{d}/s.txt'')')",
        "SELECT * FROM duckdb_settings()",
        "SELECT current_setting('motherduck_token') AS t",
        f"ATTACH '{d}/other.duckdb' AS o (READ_ONLY)",
        "DETACH o",
        f"COPY (SELECT 1) TO '{d}/out.csv'",
        f"COPY customers FROM '{d}/s.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET enable_external_access = true",
        "RESET enable_external_access",
        f"EXPORT DATABASE '{d}/dump'",
        f"IMPORT DATABASE '{d}/dump'",
        f"SELECT 1 AS n; SELECT * FROM read_text('{d}/s.txt')",
    ]


OTHER_VECTORS = [
    ("postgres", "SELECT pg_read_file('/etc/passwd') AS f"),
    ("postgres", "SELECT * FROM pg_read_file('/etc/passwd') AS f"),
    ("postgres", "SELECT pg_ls_dir('/') AS f"),
    ("postgres", "SELECT lo_import('/etc/passwd') AS f"),
    ("postgres", "SELECT query_to_xml('SELECT * FROM hr.salaries', true, true, '') AS x"),
    ("postgres", "SELECT table_to_xml('hr.salaries', true, true, '') AS x"),
    ("postgres", "SELECT * FROM dblink('host=elsewhere', 'SELECT 1') AS t(a int)"),
    ("postgres", "SELECT set_config('role', 'admin', false) AS r"),
    ("postgres", "SELECT a INTO copy_of_t FROM t"),
    ("postgres", "COPY t FROM PROGRAM 'id'"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd') AS f"),
    ("mysql", "SELECT a FROM t INTO OUTFILE '/tmp/x'"),
    ("snowflake", "SELECT $1 FROM @my_stage"),
    ("snowflake", "SELECT $1 FROM @my_stage/data.csv"),
    ("snowflake", "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))"),
    ("snowflake", "SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())"),
    ("snowflake", "SELECT GET_PRESIGNED_URL(@my_stage, 'x.csv') AS u"),
    ("bigquery", "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')"),
]


def _model(dialect: str) -> SemanticModel:
    return SemanticModel.model_validate(
        {
            "lens": "l",
            "dialect": dialect,
            "entities": [
                {
                    "name": "customers",
                    "source": {"connection": "wh", "table": "customers"},
                    "fields": [{"name": "customer_id", "type": "number"}],
                }
            ],
        }
    )


def _refused_on_every_guard_door(sql: str, dialect: str) -> None:
    model = _model(dialect)
    lens = sql_guard.check(sql, model)
    trusted = sql_guard.check(sql, model, trust_tables=True)
    probe = sql_guard.check(sql, model, trust_tables=True, allow_star=True)
    for door, r in (("lens", lens), ("certified", trusted), ("connection", probe)):
        assert not r.ok, f"{door} door served: {sql}"
    assert external_access.refusal(sql, dialect), sql


# ── the guard, on every door ─────────────────────────────────────────────────


def test_every_duckdb_vector_is_refused_by_the_guard(tmp_path: Path) -> None:
    for sql in duckdb_vectors(tmp_path):
        _refused_on_every_guard_door(sql, "duckdb")


@pytest.mark.parametrize(("dialect", "sql"), OTHER_VECTORS)
def test_other_warehouses_ways_out_are_refused(dialect: str, sql: str) -> None:
    _refused_on_every_guard_door(sql, dialect)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("duckdb", "SELECT customer_id FROM customers"),
        ("duckdb", "SELECT g.m FROM generate_series(1, 3) AS g(m)"),
        ("duckdb", "SELECT r.range FROM range(3) AS r"),
        ("duckdb", "SELECT u FROM unnest([1, 2]) AS t(u)"),
        ("duckdb", "SELECT c.customer_id, s.k FROM customers c, LATERAL (SELECT 1 AS k) s"),
        ("postgres", "SELECT d FROM generate_series(1, 3) AS d"),
        ("snowflake", "SELECT f.value FROM customers c, TABLE(FLATTEN(input => c.tags)) f"),
        ("snowflake", "SELECT seq4() AS n FROM TABLE(GENERATOR(ROWCOUNT => 3))"),
        ("bigquery", "SELECT x FROM UNNEST([1, 2]) AS x"),
    ],
)
def test_reads_that_stay_inside_still_pass(dialect: str, sql: str) -> None:
    assert external_access.refusal(sql, dialect) is None, sql
    r = sql_guard.check(sql, _model(dialect), trust_tables=True, allow_star=True)
    assert r.ok, r.reason


# Row generators: the only table functions a statement may call. Each reads
# nothing but the values the query hands it.
_DUCKDB_GENERATORS = {"range", "generate_series", "unnest"}


def test_tripwire_every_duckdb_table_function_is_refused() -> None:
    """A new DuckDB version that adds a reader cannot reopen the door: every
    table function the installed engine has is refused in FROM, LATERAL and a
    join, except the row generators named above."""
    con = duckdb.connect()
    names = sorted(
        {
            str(n)
            for (n,) in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() "
                "WHERE function_type IN ('table', 'table_macro')"
            ).fetchall()
        }
    )
    assert any(n.startswith("read_") for n in names), "enumeration found no readers"
    served = []
    for name in names:
        if name.lower() in _DUCKDB_GENERATORS:
            continue
        call = f"\"{name}\"('x')" if not name.isidentifier() else f"{name}('x')"
        for sql in (
            f"SELECT * FROM {call}",
            f"SELECT * FROM customers AS c, LATERAL {call} AS f",
            f"SELECT * FROM customers AS c JOIN {call} AS f ON true",
        ):
            if external_access.refusal(sql, "duckdb") is None:
                served.append(sql)
    assert not served, served


# ── the connector, whatever path sent the SQL ────────────────────────────────


def test_duckdb_connector_refuses_and_never_returns_the_file(secret_files: Path) -> None:
    conn = DuckDBConnector(settings.duckdb_jaffle_path)
    for sql in duckdb_vectors(secret_files):
        with pytest.raises(ExternalAccessRefused):
            conn.execute(sql)
        assert not conn.dry_run(sql).valid
    assert conn.execute("SELECT count(*) AS n FROM customers").rows[0][0] > 0


def test_local_duckdb_engine_is_contained_under_the_statement_check(secret_files: Path) -> None:
    """The engine-level backstop: on the raw session (no statement check at all)
    nothing outside the database file is reachable and the setting cannot be
    turned back on."""
    conn = DuckDBConnector(settings.duckdb_jaffle_path)
    d = secret_files
    raw = [
        f"SELECT * FROM read_json_auto('{d}/s.json')",
        f"SELECT content FROM read_text('{d}/s.txt')",
        f"SELECT * FROM '{d}/s.csv'",
        f"SELECT * FROM glob('{d}/*')",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
        f"ATTACH '{d}/other.duckdb' AS o (READ_ONLY)",
        f"COPY (SELECT 1) TO '{d}/out.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
    ]
    for sql in raw:
        with conn._session() as con, pytest.raises(duckdb.Error):
            con.execute(sql).fetchall()
    assert not (d / "out.csv").exists()
    with conn._session() as con:
        assert con.execute("SELECT count(*) FROM customers").fetchone()[0] > 0  # type: ignore[index]


def test_motherduck_keeps_its_open_and_gets_the_statement_check(
    monkeypatch: pytest.MonkeyPatch, secret_files: Path
) -> None:
    """MotherDuck cannot open contained (its extension loads at open), so its
    open is left as it was and the statement check is what stands: a vector is
    refused before any cursor is taken, and a normal read goes through."""
    from services.connectors import duckdb as duckdb_connector

    live = duckdb.connect()  # external access on, as a MotherDuck session is
    live.execute("CREATE TABLE customers AS SELECT 1 AS customer_id")
    taken: list[str] = []

    def cursor(path: str, read_only: bool, token: str | None) -> tuple[object, object]:
        taken.append(path)
        return live.cursor(), lambda: None

    monkeypatch.setattr(duckdb_connector, "_motherduck_cursor", cursor)
    conn = DuckDBConnector("md:dst_demo", token="t")
    for sql in duckdb_vectors(secret_files):
        with pytest.raises(ExternalAccessRefused):
            conn.execute(sql)
    assert taken == []
    assert conn.execute("SELECT customer_id FROM customers").rows == [[1]]
    assert taken == ["md:dst_demo"]


@pytest.mark.parametrize(
    "sql",
    [s for d, s in OTHER_VECTORS if d == "postgres"]
    + ["SELECT 1 AS n; SET default_transaction_read_only = off"],
)
def test_postgres_connector_refuses_before_connecting(sql: str) -> None:
    conn = PostgresConnector(host="unreachable.invalid", database="d", user="u", password="p")
    with pytest.raises(ExternalAccessRefused):
        conn.execute(sql)
    assert not conn.dry_run(sql).valid


@pytest.mark.parametrize("sql", [s for d, s in OTHER_VECTORS if d == "mysql"])
def test_mysql_connector_refuses_before_connecting(sql: str) -> None:
    conn = MySQLConnector(host="unreachable.invalid", database="d", user="u", password="p")
    with pytest.raises(ExternalAccessRefused):
        conn.execute(sql)
    assert not conn.dry_run(sql).valid


# ── the HTTP doors: an admin token and a caller key ──────────────────────────

client = TestClient(app)


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


@pytest.fixture
def org_keys() -> object:
    """A throwaway org: its admin token and a dst_ key granted the jaffle lens."""
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Fence') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    try:
        ah = {"Authorization": f"Bearer {raw}"}
        bundle = jaffle_customer_value_bundle()
        bundle.config.access.allow = [AccessRule(caller="agent1")]
        created = client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah)
        assert created.status_code == 201, created.text
        assert client.post("/mgmt/lenses/customer_value/publish", headers=ah).status_code == 200
        assert client.post("/mgmt/callers", json={"name": "agent1"}, headers=ah).status_code == 201
        key = client.post("/mgmt/callers/agent1/keys", headers=ah).json()["key"]
        yield raw, key
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _probe(auth: str, **body: object) -> object:
    return client.post("/v1/sql", json=body, headers={"Authorization": f"Bearer {auth}"})


@needs_db
def test_both_doors_refuse_every_vector(org_keys, secret_files: Path) -> None:
    admin, key = org_keys
    for sql in duckdb_vectors(secret_files):
        for auth, scope in ((admin, {"connection": "jaffle"}), (key, {"lens": "customer_value"})):
            r = _probe(auth, sql=sql, **scope)
            assert r.status_code == 400, (scope, sql, r.text)
            assert "SQL refused" in r.json()["detail"], (scope, sql)
            assert SECRET not in r.text

    ok = _probe(admin, connection="jaffle", sql="SELECT * FROM customers", limit=2)
    assert ok.status_code == 200, ok.text
    ok = _probe(key, lens="customer_value", sql="SELECT customer_id FROM customers", limit=2)
    assert ok.status_code == 200, ok.text
