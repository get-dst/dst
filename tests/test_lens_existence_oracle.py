"""The caller surface never answers "does this lens exist".

403-for-denied vs 404-for-absent on GET /v1/lenses/{name} is a perfectly
separable existence oracle, and if the route skips the rate limiter any caller
key can enumerate the org's lens inventory at unlimited speed. Lens names
describe the business, so names leaking IS data leaking. Both doors go through
`_authorized_bundle`: one 404 with a byte-identical body for absent and denied,
throttled like queries.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import AccessRule
from services.db.session import org_session
from services.governance import credentials, ratelimit
from services.lenses import store
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


@pytest.fixture()
def probe_org():
    """An org with one published lens and two callers: 'granted' is on the
    allow-list, 'outsider' is not. per_caller_rpm=2 so the throttle tests
    stay cheap."""
    ratelimit.reset()
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('OracleTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    keys: dict[str, str] = {}
    try:
        with org_session(org) as s:
            for name in ("granted", "outsider"):
                keys[name] = credentials.issue_key(s, credentials.create_caller(s, name))
            bundle = jaffle_customer_value_bundle()
            bundle.config.rate_limit.per_caller_rpm = 2
            bundle.config.access.allow = [AccessRule(caller="granted")]
            store.create_lens(s, bundle)
            store.publish(s, "customer_value")
        yield keys
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM audit_log WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM api_key WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM caller WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _get(name: str, key: str):
    return client.get(f"/v1/lenses/{name}", headers={"Authorization": f"Bearer {key}"})


@needs_db
def test_a_denied_lens_is_indistinguishable_from_a_nonexistent_one(probe_org) -> None:
    denied = _get("customer_value", probe_org["outsider"])
    absent = _get("no_such_lens_xyz", probe_org["outsider"])
    assert denied.status_code == absent.status_code == 404
    assert denied.json()["detail"] == absent.json()["detail"]


@needs_db
def test_an_allowed_caller_still_gets_the_lens(probe_org) -> None:
    r = _get("customer_value", probe_org["granted"])
    assert r.status_code == 200
    assert "customer_value" in r.text


@needs_db
def test_the_describe_endpoint_is_rate_limited(probe_org) -> None:
    codes = [_get("customer_value", probe_org["granted"]).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@needs_db
def test_the_rate_limit_bucket_is_per_caller(probe_org) -> None:
    for _ in range(3):
        _get("customer_value", probe_org["granted"])
    # A different caller's bucket is untouched — but 'outsider' isn't granted,
    # so the parity check is: it still gets the uniform 404, not a 429.
    assert _get("customer_value", probe_org["outsider"]).status_code == 404


@needs_db
def test_certified_route_uses_the_same_door(probe_org) -> None:
    """The certified-library route had the same hand-rolled prologue — the
    class fix is one door, so a denied caller gets the same uniform 404 there."""
    h = {"Authorization": f"Bearer {probe_org['outsider']}"}
    denied = client.get("/v1/lenses/customer_value/certified", headers=h)
    absent = client.get("/v1/lenses/no_such_lens_xyz/certified", headers=h)
    assert denied.status_code == absent.status_code == 404
    assert denied.json()["detail"] == absent.json()["detail"]
