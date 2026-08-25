"""Warehouse attribution: the person reaches the warehouse's own logs.

dst connects as one service account, so without this the warehouse sees only
that account — an agent's SQL is anonymous in QUERY_HISTORY / pg_stat_activity. The
tag carries principal, agent and the request_id that joins back to request_log.

Self-asserted, so these prove the tag is DELIVERED to the warehouse session, which
is exactly what the feature guarantees — not that identity is unforgeable (it isn't;
that is per-user warehouse identity, a different control).

The Postgres test is LIVE: it points a warehouse connector at the local Postgres and
reads `application_name` back from the session, so the tag is verified end to end
against a real database, not a mock.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, make_url, text

from services.config import settings
from services.connectors.postgres import PostgresConnector
from services.runtime.attribution import Attribution, application_name, attributed, query_tag


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


def _attr(rid: str = "req_abc123") -> Attribution:
    return Attribution(principal="antti", agent="claude-desktop", request_id=rid, org_id="o1")


def test_tag_is_none_when_unbound() -> None:
    """No request in flight ⇒ no tag. A connector built outside a governed request
    (introspection, profiling) must not stamp a stale identity."""
    assert query_tag() is None
    assert application_name() == "dst"


def test_tag_carries_the_triple_and_resets_on_exit() -> None:
    assert query_tag() is None
    with attributed(_attr()):
        tag = query_tag()
        assert tag is not None
        assert '"principal":"antti"' in tag
        assert '"agent":"claude-desktop"' in tag
        assert '"rid":"req_abc123"' in tag
        assert application_name() == "dst:req_abc123"
    # The reset is the load-bearing guarantee — a pooled connection must not attach
    # this caller's identity to the next caller's query.
    assert query_tag() is None
    assert application_name() == "dst"


@needs_db
def test_postgres_application_name_reaches_the_live_session() -> None:
    """End to end against the real database: the tag is on the wire, not just formatted."""
    url = make_url(settings.database_admin_url)
    conn = PostgresConnector(
        host=url.host or "localhost",
        port=url.port or 5432,
        database=url.database or "dst",
        user=url.username or "dst",
        password=url.password or "",
        sslmode="disable",
    )
    with attributed(_attr("req_live_pg")):
        result = conn.execute("SELECT current_setting('application_name') AS app", read_only=True)
    app = result.rows[0][0]
    assert app == "dst:req_live_pg", f"application_name not delivered to the session: {app!r}"

    # And unbound, the connector connects without a stale tag.
    result2 = conn.execute("SELECT current_setting('application_name') AS app", read_only=True)
    assert result2.rows[0][0] == "dst"


def test_snowflake_passes_query_tag_to_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline: assert the tag reaches snowflake.connector.connect (no live Snowflake
    in CI). Same shape as the keypair test — the guarantee is what reaches the driver."""
    from typing import Any

    import snowflake.connector

    from services.connectors.snowflake import SnowflakeConnector

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        snowflake.connector, "connect", lambda **kw: seen.update(kw) or _FakeSnowflakeConn()
    )
    conn = SnowflakeConnector(account="a", user="u", password="p", warehouse="w", database="d")
    with attributed(_attr("req_sf")):
        try:
            conn.introspect()
        except Exception:
            pass  # the fake connection can't really introspect; only connect() is needed
    params = seen.get("session_parameters", {})
    assert "QUERY_TAG" in params, "no QUERY_TAG handed to Snowflake"
    assert '"rid":"req_sf"' in params["QUERY_TAG"]
    assert re.search(r'"principal":"antti"', params["QUERY_TAG"])


class _FakeSnowflakeConn:
    def cursor(self) -> object:
        raise RuntimeError("stop after connect")

    def close(self) -> None:
        pass
