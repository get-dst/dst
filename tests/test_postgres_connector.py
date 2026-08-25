"""PostgreSQL connector contract tests.

The generic contract test is opt-in against an arbitrary server:
DST_TEST_POSTGRES=1 uv run pytest tests/test_postgres_connector.py
(params from the standard PG* env vars).

The schema-discovery tests are NOT opt-in: the suite already requires Postgres
(conftest builds the scratch database), so the shape — tables outside
the default schema introspecting to nothing — is pinned against a real server on
every run.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text

from services.connectors.postgres import PostgresConnector
from services.contracts.protocols import Connector

run_pg = pytest.mark.skipif(
    not os.environ.get("DST_TEST_POSTGRES"),
    reason="set DST_TEST_POSTGRES=1 (+ PG* env vars) to run",
)


@run_pg
def test_postgres_introspect_and_query() -> None:
    conn = PostgresConnector(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
    )
    assert isinstance(conn, Connector)

    snap = conn.introspect()
    assert snap.dialect == "postgres"

    res = conn.execute("SELECT 1 AS n", row_limit=10)
    assert res.columns == ["n"]
    assert res.rows == [[1]]


# ── the Postgres shape of the empty-snapshot failure ─────────────────────────
# `schema` defaulted to "public", so a warehouse whose tables live in another
# schema introspected to an empty snapshot, silently. Undeclared now means
# EVERY non-system schema.

_SCHEMA = "dst_probe_spider"


@pytest.fixture
def pg_params() -> Iterator[dict[str, object]]:
    """libpq params for the suite's own scratch database, with one table planted
    in a non-default schema and one in public."""
    admin = os.environ["DATABASE_ADMIN_URL"]
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
        c.execute(text(f'CREATE TABLE "{_SCHEMA}".player (id integer PRIMARY KEY, name text)'))
        c.execute(text("CREATE TABLE IF NOT EXISTS public.dst_probe_top (x bigint)"))
    url = urlparse(admin.replace("postgresql+psycopg://", "postgresql://"))
    try:
        yield {
            "host": url.hostname or "localhost",
            "port": url.port or 5432,
            "database": (url.path or "/").lstrip("/"),
            "user": url.username or "",
            "password": url.password or "",
        }
    finally:
        with engine.connect() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
            c.execute(text("DROP TABLE IF EXISTS public.dst_probe_top"))
        engine.dispose()


def test_introspect_spans_every_non_system_schema(pg_params: dict[str, object]) -> None:
    snap = PostgresConnector(**pg_params).introspect()  # type: ignore[arg-type]
    names = {t.name for t in snap.tables}
    assert f"{_SCHEMA}.player" in names  # the table the old default could not see
    assert "public.dst_probe_top" in names
    assert not any(n.startswith(("pg_", "information_schema.")) for n in names)
    assert _SCHEMA in snap.schemas_searched and "public" in snap.schemas_searched
    assert "pg_catalog" not in snap.schemas_searched


def test_declared_schema_still_scopes_reads(pg_params: dict[str, object]) -> None:
    snap = PostgresConnector(**pg_params, schema=_SCHEMA).introspect()  # type: ignore[arg-type]
    assert [t.name for t in snap.tables] == [f"{_SCHEMA}.player"]
    assert snap.schemas_searched == [_SCHEMA]


def test_profiles_are_named_exactly_as_introspect_names_them(
    pg_params: dict[str, object],
) -> None:
    """The stored-profile join key is the table name — a profile keyed
    `public.player` against a snapshot keyed `spider.player` silently loses
    every profile fact."""
    conn = PostgresConnector(**pg_params)  # type: ignore[arg-type]
    profiled = {p.table for p in conn.profile_catalog()}
    assert profiled <= {t.name for t in conn.introspect().tables}
    assert f"{_SCHEMA}.player" in profiled
