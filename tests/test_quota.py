"""Daily quotas on the data plane: per-caller per lens, and the install-wide cap.

Both count the answers actually served (request_log, rolling 24 h) at the same
prologue as the per-minute limiter, and refuse the same way — 429 with a
Retry-After, audited as a deny. A decline never eats budget: the deny rows live
in audit_log, and the count reads request_log only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.auth.tokens import hash_token, new_caller_key
from services.config import settings
from services.contracts.lens_config import AccessRule
from services.db.session import org_session
from services.governance import quota, ratelimit
from services.lenses import store
from services.lenses.demo import LENS_NAME, jaffle_customer_value_bundle


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
_admin = create_engine(settings.database_admin_url)

CALLER = "visitor@example.com"


@pytest.fixture(autouse=True)
def _clean_budget() -> Iterator[None]:
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def client(live_client: TestClient) -> TestClient:
    return live_client


@pytest.fixture
def seeded() -> Iterator[tuple[uuid.UUID, str]]:
    """An org with the demo lens published to group `demo` at 3 answers/day,
    and a demo caller holding a live key. Yields (org_id, raw_key)."""
    raw = new_caller_key()
    with _admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('QuotaTest') RETURNING id")
        ).scalar_one()
        cid = c.execute(
            text(
                "INSERT INTO caller (org_id, name, type, groups) "
                "VALUES (:o, :n, 'demo', ARRAY['demo']) RETURNING id"
            ),
            {"o": org, "n": CALLER},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO api_key (org_id, caller_id, key_hash, prefix) VALUES (:o, :c, :h, :p)"
            ),
            {"o": org, "c": cid, "h": hash_token(raw), "p": raw[:12]},
        )
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(group="demo")]
    bundle.config.rate_limit.per_caller_rpd = 3
    with org_session(org) as session:
        store.create_lens(session, bundle)
        store.publish(session, LENS_NAME)
    try:
        yield uuid.UUID(str(org)), raw
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _served(org: uuid.UUID, n: int, *, caller: str = CALLER, age: timedelta | None = None) -> None:
    """n governed answers on the log, `age` ago (default: now)."""
    at = datetime.now(UTC) - (age or timedelta(0))
    with org_session(org) as session:
        for _ in range(n):
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, question, "
                    "status, created_at) VALUES (:o, :r, :l, :c, 'q', 'ok', :t)"
                ),
                {"o": org, "r": f"r-{uuid.uuid4()}", "l": LENS_NAME, "c": caller, "t": at},
            )


@needs_db
def test_under_quota_is_served(client: TestClient, seeded: tuple[uuid.UUID, str]) -> None:
    org, raw = seeded
    _served(org, 2)
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200


@needs_db
def test_at_quota_is_429_with_retry_after(
    client: TestClient, seeded: tuple[uuid.UUID, str]
) -> None:
    org, raw = seeded
    _served(org, 3, age=timedelta(hours=1))
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 429
    assert "daily quota" in r.json()["detail"]
    # The oldest counted row is an hour old: the budget frees in ~23 h, not "a day".
    retry = int(r.headers["Retry-After"])
    assert 22 * 3600 < retry <= 23 * 3600 + 1
    with _admin.begin() as c:
        reasons = (
            c.execute(
                text("SELECT reason FROM audit_log WHERE org_id = :o AND decision = 'deny'"),
                {"o": org},
            )
            .scalars()
            .all()
        )
    assert "daily quota exceeded" in reasons


@needs_db
def test_yesterday_does_not_count(client: TestClient, seeded: tuple[uuid.UUID, str]) -> None:
    org, raw = seeded
    _served(org, 10, age=timedelta(hours=25))
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200


@needs_db
def test_another_callers_answers_do_not_count(
    client: TestClient, seeded: tuple[uuid.UUID, str]
) -> None:
    org, raw = seeded
    _served(org, 10, caller="someone-else")
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200


@needs_db
def test_org_cap_is_the_kill_switch(
    client: TestClient, seeded: tuple[uuid.UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everyone under their own quota, the org over its cap: refused, and the
    detail says it is the deployment's cap, not the caller's."""
    org, raw = seeded
    _served(org, 5, caller="a")
    _served(org, 5, caller="b")
    monkeypatch.setattr(settings, "daily_request_cap", 10)
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 429
    assert "daily cap" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) > 0
    monkeypatch.setattr(settings, "daily_request_cap", 0)
    assert (
        client.get(
            f"/v1/lenses/{LENS_NAME}", headers={"Authorization": f"Bearer {raw}"}
        ).status_code
        == 200
    )


@needs_db
def test_probe_rows_are_not_governed_usage(seeded: tuple[uuid.UUID, str]) -> None:
    org, _ = seeded
    with org_session(org) as session:
        session.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, status, "
                "generator_tier) VALUES (:o, 'p', :l, :c, 'q', 'ok', 'probe')"
            ),
            {"o": org, "l": LENS_NAME, "c": CALLER},
        )
    assert quota.usage(org, caller=CALLER).served == 0


def test_zero_cap_means_no_quota() -> None:
    assert not quota.exceeded(0, quota.Usage(served=10**6, oldest=None))
    assert quota.exceeded(1, quota.Usage(served=1, oldest=None))


def test_retry_after_counts_down_from_the_oldest_row() -> None:
    now = datetime.now(UTC)
    use = quota.Usage(served=1, oldest=now - timedelta(hours=23))
    assert 3600 <= use.retry_after(now=now) <= 3601
    assert quota.Usage(served=0, oldest=None).retry_after() == quota.WINDOW_SECONDS
