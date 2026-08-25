"""POST /v1/sql — the governed read-only probe.

The verb exists because callers who have already run `dst introspect`
successfully want rows, not schema, and had no governed way to get them. So what
these tests pin is the governance, not the SQL: SELECT-only, allow-list on the
lens door, admin-only on the connection door, a row cap that admits when it
fired, and a request_log row — a probe outside the audit trail is the thing this
replaces.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import AccessRule
from services.lenses.demo import jaffle_customer_value_bundle

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
def org_admin() -> object:
    """A throwaway org + admin token; dropped (cascading) after the test."""
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Probe') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    try:
        yield org, raw
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _probe(auth: str, **body: object) -> object:
    return client.post("/v1/sql", json=body, headers={"Authorization": f"Bearer {auth}"})


# ── the connection door (authoring) ───────────────────────────────────────────


@needs_db
def test_admin_probe_returns_rows_and_a_request_id(org_admin) -> None:
    _org, raw = org_admin
    r = _probe(raw, connection="jaffle", sql="SELECT * FROM customers", limit=3)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "customer_id" in d["columns"]
    assert d["row_count"] == 3 and len(d["rows"]) == 3
    # SELECT * is the point of the verb, not a hole: the admin holds this
    # connection's credential and can already profile every column.
    assert d["request_id"].startswith("req_") and d["scope"] == "sql:jaffle"


@needs_db
def test_row_cap_says_when_it_fired(org_admin) -> None:
    _org, raw = org_admin
    d = _probe(raw, connection="jaffle", sql="SELECT customer_id FROM customers", limit=2).json()
    assert d["row_count"] == 2 and d["truncated"] is True
    d = _probe(raw, connection="jaffle", sql="SELECT count(*) AS n FROM customers").json()
    assert d["row_count"] == 1 and d["truncated"] is False


@needs_db
def test_limit_above_the_hard_cap_is_refused(org_admin) -> None:
    _org, raw = org_admin
    assert _probe(raw, connection="jaffle", sql="SELECT 1 AS n", limit=5000).status_code == 422


@needs_db
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE customers SET customer_name = 'x'",
        "INSERT INTO customers (customer_id) VALUES (1)",
        "CREATE TABLE t AS SELECT 1 AS n",
        "SELECT 1 AS n; DROP TABLE customers",
        "WITH x AS (DELETE FROM customers RETURNING 1) SELECT * FROM x",
    ],
)
def test_non_select_is_refused(org_admin, sql: str) -> None:
    _org, raw = org_admin
    r = _probe(raw, connection="jaffle", sql=sql)
    assert r.status_code == 400, r.text
    assert "SQL refused" in r.json()["detail"]


@needs_db
def test_probe_lands_in_the_request_log(org_admin) -> None:
    """The reason the verb runs server-side at all: an authoring decision's
    evidence sits in the audit trail next to the answers it shaped."""
    org, raw = org_admin
    d = _probe(raw, connection="jaffle", sql="SELECT customer_id FROM customers", limit=2).json()
    with create_engine(settings.database_admin_url).connect() as c:
        row = c.execute(
            text(
                "SELECT lens, caller, status, row_count, sql, generator_tier "
                "FROM request_log WHERE org_id = :o AND request_id = :r"
            ),
            {"o": org, "r": d["request_id"]},
        ).first()
    assert row is not None
    assert row[0] == "sql:jaffle" and row[2] == "ok" and row[3] == 2
    assert row[4] == "SELECT customer_id FROM customers" and row[5] == "probe"


@needs_db
def test_refused_sql_is_logged_too(org_admin) -> None:
    org, raw = org_admin
    r = _probe(raw, connection="jaffle", sql="DROP TABLE customers")
    assert r.status_code == 400
    with create_engine(settings.database_admin_url).connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM request_log "
                "WHERE org_id = :o AND status = 'rejected' AND generator_tier = 'probe'"
            ),
            {"o": org},
        ).scalar()
    assert n == 1


@needs_db
def test_unknown_connection_is_a_404_naming_the_fix(org_admin) -> None:
    _org, raw = org_admin
    r = _probe(raw, connection="nope", sql="SELECT 1 AS n")
    assert r.status_code == 404
    assert "dst.yaml" in r.json()["detail"] and "dst apply" in r.json()["detail"]


@needs_db
def test_exactly_one_scope(org_admin) -> None:
    _org, raw = org_admin
    assert _probe(raw, sql="SELECT 1 AS n").status_code == 400
    assert _probe(raw, connection="jaffle", lens="l", sql="SELECT 1 AS n").status_code == 400


# ── the lens door (serving; what the MCP tool uses) ───────────────────────────


@needs_db
def test_lens_door_enforces_the_allow_list_and_the_lens_scope(org_admin) -> None:
    org, raw = org_admin
    ah = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(caller="agent1")]
    assert (
        client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah).status_code
        == 201
    )
    assert client.post("/mgmt/lenses/customer_value/publish", headers=ah).status_code == 200
    for name in ("agent1", "other"):
        assert client.post("/mgmt/callers", json={"name": name}, headers=ah).status_code == 201
    granted = client.post("/mgmt/callers/agent1/keys", headers=ah).json()["key"]
    denied = client.post("/mgmt/callers/other/keys", headers=ah).json()["key"]

    ok = _probe(granted, lens="customer_value", sql="SELECT customer_id FROM customers", limit=2)
    assert ok.status_code == 200, ok.text
    assert ok.json()["scope"] == "customer_value"

    # deny-by-default holds on this door exactly as it does on /query — and it
    # answers with the uniform 404, never confirming existence
    assert (
        _probe(denied, lens="customer_value", sql="SELECT customer_id FROM customers").status_code
        == 404
    )
    # a caller key may not probe a whole connection — that is an org credential
    r = _probe(granted, connection="jaffle", sql="SELECT 1 AS n")
    assert r.status_code == 403 and "admin token" in r.json()["detail"]
    # ...and inside the lens the allow-list still governs: no unmodelled table,
    # and no star (a star is how a column the lens does not expose comes back).
    assert (
        _probe(granted, lens="customer_value", sql="SELECT x FROM raw_secrets").status_code == 400
    )
    star = _probe(granted, lens="customer_value", sql="SELECT * FROM customers")
    assert star.status_code == 400 and "SELECT *" in star.json()["detail"]

    # the probe is attributed to the caller who ran it
    with create_engine(settings.database_admin_url).connect() as c:
        callers = (
            c.execute(
                text(
                    "SELECT DISTINCT caller FROM request_log "
                    "WHERE org_id = :o AND lens = 'customer_value' AND generator_tier = 'probe'"
                ),
                {"o": org},
            )
            .scalars()
            .all()
        )
    assert callers == ["agent1"]


@needs_db
def test_unpublished_lens_is_a_404(org_admin) -> None:
    _org, raw = org_admin
    assert _probe(raw, lens=f"nope{uuid.uuid4().hex[:6]}", sql="SELECT 1 AS n").status_code == 404


# ── the door people actually use ─────────────────────────────────────────────


@needs_db
def test_the_cli_verb_prints_rows_and_reports_a_refusal(org_admin, monkeypatch, capsys) -> None:
    """End-to-end through `dst sql`, since a governed probe nobody can invoke
    is not a replacement for opening DuckDB."""
    import sys
    from urllib.parse import urlsplit

    import httpx

    from services.cli.main import main as cli_main

    _org, raw = org_admin
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, headers=None, json=None, timeout=None, params=None: client.post(
            urlsplit(url).path, headers=headers, json=json
        ),
    )
    on_jaffle = ["--connection", "jaffle", "--token", raw]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dst",
            "sql",
            "SELECT customer_id, customer_name FROM customers",
            *on_jaffle,
            "--limit",
            "2",
        ],
    )
    assert cli_main() == 0
    out = capsys.readouterr().out
    assert "customer_id" in out and "customer_name" in out
    assert "(2 rows; capped" in out  # the cap admits it fired; --limit is the fix

    monkeypatch.setattr(sys, "argv", ["dst", "sql", "DROP TABLE customers", *on_jaffle])
    assert cli_main() == 1
    assert "SQL refused" in capsys.readouterr().err

    # A refusal that names only what is disallowed sends the caller to open the
    # warehouse directly. The actual need is the table list, and `SHOW TABLES`
    # is the shape that asks for it here, so the refusal names the verb that
    # answers it.
    monkeypatch.setattr(sys, "argv", ["dst", "sql", "SHOW TABLES", *on_jaffle])
    assert cli_main() == 1
    err = capsys.readouterr().err
    assert "only SELECT queries are allowed" in err
    assert "dst introspect --connection jaffle" in err
