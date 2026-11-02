"""The Cube client against a fake Cube: the wire shapes it reads (captured
from a real Cube Core 1.6 over the same calls), the continue-wait polling, the
security-context token, and the failure mode every Cube answer maps to."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
import time_machine

from services.contracts.cube_query import CubeFilter, CubeQuery, TimeDimension
from services.contracts.failure_modes import FailureMode
from services.cube.client import CubeClient, CubeError, failure_mode
from services.cube.context import TOKEN_LIFETIME, CubeContext
from services.governance.credentials import CallerIdentity

SECRET = "scratch-secret-scratch-secret-scratch"

# ── a fake Cube, shaped like the real one ─────────────────────────────────

META: dict[str, Any] = {
    "cubes": [
        {
            "name": "orders",
            "type": "cube",
            "title": "Orders",
            "meta": {"dst": {"grain": "one row per order", "key": ["id"]}},
            "measures": [
                {"name": "orders.count", "type": "number", "aggType": "count"},
                {
                    "name": "orders.revenue",
                    "type": "number",
                    "aggType": "sum",
                    "meta": {
                        "dst": {"additivity": "flow", "aliases": ["sales"], "clock": "ordered_at"}
                    },
                },
            ],
            "dimensions": [
                {"name": "orders.status", "type": "string", "meta": {"dst": {"dictionary": True}}},
                {"name": "orders.ordered_at", "type": "time"},
            ],
            "segments": [
                {"name": "orders.paid", "meta": {"dst": {"meaning": "orders already paid"}}}
            ],
            "joins": [{"name": "customers", "relationship": "many_to_one"}],
        },
        {
            "name": "customers",
            "type": "cube",
            "measures": [],
            "dimensions": [{"name": "customers.country", "type": "string"}],
            "segments": [],
        },
        {
            "name": "islands",
            "type": "cube",
            "measures": [{"name": "islands.count", "type": "number", "aggType": "count"}],
            "dimensions": [],
            "segments": [],
        },
        {
            "name": "orders_view",
            "type": "view",
            "measures": [
                {
                    "name": "orders_view.count",
                    "type": "number",
                    "aggType": "count",
                    "aliasMember": "orders.count",
                },
                {
                    "name": "orders_view.revenue",
                    "type": "number",
                    "aggType": "sum",
                    "aliasMember": "orders.revenue",
                    "meta": {
                        "dst": {"additivity": "flow", "aliases": ["sales"], "clock": "ordered_at"}
                    },
                },
            ],
            "dimensions": [
                {"name": "orders_view.status", "type": "string", "aliasMember": "orders.status"},
                {
                    "name": "orders_view.ordered_at",
                    "type": "time",
                    "aliasMember": "orders.ordered_at",
                },
                {
                    "name": "orders_view.country",
                    "type": "string",
                    "aliasMember": "customers.country",
                },
            ],
            "segments": [{"name": "orders_view.paid", "aliasMember": "orders.paid"}],
        },
    ]
}

SQL_PAYLOAD: dict[str, Any] = {
    "sql": {
        "external": False,
        "sql": [
            'SELECT sum("orders".amount) "orders_view__revenue" FROM orders AS "orders" '
            'WHERE ("orders".status = ?) LIMIT 5',
            ["paid"],
        ],
        "order": {"orders_view.revenue": "desc"},
        "preAggregations": [],
        "dataSource": "default",
    }
}

LOAD_PAYLOAD: dict[str, Any] = {
    "query": {"measures": ["orders_view.revenue"], "limit": 5, "timezone": "UTC"},
    "lastRefreshTime": "2026-10-29T09:40:21.260Z",
    "annotation": {"measures": {"orders_view.revenue": {"type": "number"}}},
    "dbType": "duckdb",
    "data": [{"orders_view.revenue": "40"}],
}

COMPARE_PAYLOAD: dict[str, Any] = {
    "queryType": "compareDateRangeQuery",
    "results": [
        {
            "query": {"measures": ["orders_view.revenue"]},
            "lastRefreshTime": "2026-10-29T09:40:58.512Z",
            "annotation": {},
            "dbType": "duckdb",
            "data": [{"orders_view.revenue": "50", "compareDateRange": "2026-02-01 - 2026-02-28"}],
        },
        {
            "query": {"measures": ["orders_view.revenue"]},
            "annotation": {},
            "dbType": "duckdb",
            "data": [{"orders_view.revenue": "10", "compareDateRange": "2026-01-01 - 2026-01-31"}],
        },
    ],
}


class FakeCube:
    """Answers like Cube Core: every request recorded, ``continue_waits`` of
    them answered 'Continue wait' first, a secret checked the way Cube does."""

    def __init__(
        self,
        *,
        secret: str | None = SECRET,
        continue_waits: int = 0,
        meta: dict[str, Any] | None = None,
        healthy: bool = True,
        warehouse_error: str | None = None,
    ) -> None:
        self.secret = secret
        self.continue_waits = continue_waits
        self.meta = meta or META
        self.healthy = healthy
        # What the warehouse says through Cube on /load (a dead driver: the
        # meta compiles, every query fails); None = rows come back.
        self.warehouse_error = warehouse_error
        self.requests: list[httpx.Request] = []
        self.claims: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/readyz":
            return httpx.Response(
                200 if self.healthy else 503, json={"health": "HEALTH" if self.healthy else "DOWN"}
            )
        token = request.headers.get("authorization")
        if self.secret:
            if not token:
                return httpx.Response(403, json={"error": "Authorization header isn't set"})
            try:
                # Expiry on the wall clock, as a real Cube reads it, whatever
                # clock the test pinned this process to.
                claims = jwt.decode(
                    token,
                    self.secret,
                    algorithms=["HS256"],
                    options={"verify_exp": False, "verify_iat": False},
                )
                if claims.get("exp") is not None and claims["exp"] <= _wall():
                    raise jwt.ExpiredSignatureError("Signature has expired")
                self.claims.append(claims)
            except jwt.PyJWTError:
                return httpx.Response(403, json={"error": "Invalid token"})
        if path == "/cubejs-api/v1/meta":
            return httpx.Response(200, json=self.meta)
        body = json.loads(request.content)
        query = body["query"]
        members = [*query.get("measures", []), *query.get("dimensions", [])]
        for m in members:
            cube, _, member = m.partition(".")
            if cube not in {c["name"] for c in self.meta["cubes"]}:
                return httpx.Response(
                    400, json={"error": f"Cube '{cube}' not found for path '{m}'"}
                )
            if m not in _members(self.meta, cube):
                return httpx.Response(400, json={"error": f"'{member}' not found for path '{m}'"})
        if "islands.count" in members:
            return httpx.Response(
                400, json={"error": "Can't find join path to join 'orders', 'islands'"}
            )
        for i, f in enumerate(query.get("filters", [])):
            if f.get("operator") not in _OPERATORS:
                return httpx.Response(
                    400,
                    json={"error": f'Invalid query format: "filters[{i}]" does not match'},
                )
        if path == "/cubejs-api/v1/sql":
            return httpx.Response(200, json=SQL_PAYLOAD)
        if path == "/cubejs-api/v1/dry-run":
            return httpx.Response(
                200, json={"queryType": "regularQuery", "normalizedQueries": [query]}
            )
        if path == "/cubejs-api/v1/load":
            if self.warehouse_error:
                return httpx.Response(500, json={"error": self.warehouse_error})
            if self.continue_waits > 0:
                self.continue_waits -= 1
                return httpx.Response(200, json={"error": "Continue wait"})
            if body.get("queryType") == "multi":
                return httpx.Response(200, json=COMPARE_PAYLOAD)
            return httpx.Response(200, json=LOAD_PAYLOAD)
        return httpx.Response(404, json={"error": "no such route"})


_OPERATORS = {
    "equals",
    "notEquals",
    "contains",
    "notContains",
    "startsWith",
    "endsWith",
    "gt",
    "gte",
    "lt",
    "lte",
    "set",
    "notSet",
    "inDateRange",
    "notInDateRange",
    "beforeDate",
    "afterDate",
}


def _members(meta: dict[str, Any], cube: str) -> set[str]:
    c = next(c for c in meta["cubes"] if c["name"] == cube)
    return {m["name"] for key in ("measures", "dimensions", "segments") for m in c.get(key, [])}


def client(fake: FakeCube, secret: str | None = SECRET, **kw: Any) -> CubeClient:
    return CubeClient("http://cube:4000/", secret, transport=fake.transport(), **kw)


CTX = CubeContext(org="org-1", caller="alice", groups=["finance"], lens="sales", request_id="r1")

# ── calls ─────────────────────────────────────────────────────────────────


def test_meta_parses_and_is_cached_per_context_until_the_ttl() -> None:
    fake = FakeCube()
    c = client(fake, meta_ttl=300)
    doc = c.meta(CTX)
    assert [v.name for v in doc.views()] == ["orders_view"]
    assert doc.resolve("orders_view.country") == "customers.country"
    assert c.meta(CTX) is doc, "the second read within the ttl is the cached document"
    other = CubeContext(org="org-1", caller="bob", groups=["support"])
    c.meta(other)
    assert len([r for r in fake.requests if r.url.path.endswith("/meta")]) == 2, (
        "a different group set is a different view of the model: fetched again"
    )
    c.forget_meta()
    c.meta(CTX)
    assert len([r for r in fake.requests if r.url.path.endswith("/meta")]) == 3


def test_meta_ttl_zero_asks_every_time() -> None:
    fake = FakeCube()
    c = client(fake, meta_ttl=0)
    c.meta(CTX)
    c.meta(CTX)
    assert len(fake.requests) == 2


def test_sql_returns_the_compiled_text_and_its_parameters() -> None:
    c = client(FakeCube())
    out = c.sql(CubeQuery(measures=["orders_view.revenue"], limit=5), CTX)
    assert out.sql.startswith('SELECT sum("orders".amount)') and out.params == ["paid"]
    assert out.raw["sql"]["dataSource"] == "default"


def test_the_wire_body_is_the_query_under_a_query_key_without_dst_only_fields() -> None:
    fake = FakeCube()
    q = CubeQuery(
        measures=["orders_view.revenue"],
        timeDimensions=[TimeDimension(dimension="orders_view.ordered_at", granularity="month")],
        order=[("orders_view.revenue", "desc")],
        snapshot="last",
    )
    client(fake).dry_run(q, CTX)
    body = json.loads(fake.requests[-1].content)
    assert body == {
        "query": {
            "measures": ["orders_view.revenue"],
            "timeDimensions": [{"dimension": "orders_view.ordered_at", "granularity": "month"}],
            "order": [["orders_view.revenue", "desc"]],
        }
    }


def test_load_polls_through_continue_wait_and_returns_rows() -> None:
    fake = FakeCube(continue_waits=2)
    out = client(fake).load(CubeQuery(measures=["orders_view.revenue"]), CTX, deadline=5)
    assert out.data == [{"orders_view.revenue": "40"}]
    assert out.db_type == "duckdb" and out.last_refresh_time == "2026-10-29T09:40:21.260Z"
    assert len([r for r in fake.requests if r.url.path.endswith("/load")]) == 3


def test_load_that_never_stops_waiting_is_cube_down_at_the_deadline() -> None:
    fake = FakeCube(continue_waits=10_000)
    with pytest.raises(CubeError) as exc:
        client(fake).load(CubeQuery(measures=["orders_view.revenue"]), CTX, deadline=0.3)
    assert exc.value.mode == "provider_down" and exc.value.provider == "cube"
    assert "Continue wait" in exc.value.detail or "did not return" in exc.value.detail


def test_a_compare_query_is_sent_as_a_multi_query_and_rows_carry_their_window() -> None:
    fake = FakeCube()
    q = CubeQuery(
        measures=["orders_view.revenue"],
        timeDimensions=[
            TimeDimension(
                dimension="orders_view.ordered_at",
                compareDateRange=[["2026-02-01", "2026-02-28"], ["2026-01-01", "2026-01-31"]],
            )
        ],
    )
    out = client(fake).load(q, CTX)
    assert json.loads(fake.requests[-1].content)["queryType"] == "multi"
    assert [r["compareDateRange"] for r in out.data] == [
        "2026-02-01 - 2026-02-28",
        "2026-01-01 - 2026-01-31",
    ]


def test_ready_reads_readyz() -> None:
    assert client(FakeCube()).ready().reachable
    down = client(FakeCube(healthy=False)).ready()
    assert not down.reachable and "503" in down.detail


def test_an_unreachable_cube_is_cube_down() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = CubeClient("http://cube:4000", SECRET, transport=httpx.MockTransport(refuse))
    assert not c.ready().reachable
    with pytest.raises(CubeError) as exc:
        c.meta(CTX)
    assert (exc.value.mode, exc.value.provider) == ("provider_down", "cube")


# ── the security context ──────────────────────────────────────────────────


def test_every_call_carries_a_short_lived_token_with_the_dst_claims() -> None:
    fake = FakeCube()
    client(fake).meta(CTX)
    (claims,) = fake.claims
    assert claims["dst"] == {
        "org": "org-1",
        "caller": "alice",
        "groups": ["finance"],
        "lens": "sales",
        "request_id": "r1",
    }
    assert claims["exp"] - claims["iat"] == TOKEN_LIFETIME.total_seconds()


def test_the_context_is_built_from_the_caller_identity() -> None:
    identity = CallerIdentity(
        org_id=uuid.UUID(int=7), name="bob", is_admin=False, groups=["z", "a"]
    )
    ctx = CubeContext.of(identity, lens="ops", request_id="req")
    assert ctx.org == str(uuid.UUID(int=7)) and ctx.caller == "bob"
    assert ctx.groups == ["a", "z"], "sorted, so the same membership is the same claim"
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    claims = ctx.claims(now=now)
    assert claims["iat"] == int(now.timestamp()) and claims["exp"] == claims["iat"] + 300
    decoded = jwt.decode(
        ctx.token(SECRET, now=now), SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert decoded["dst"]["lens"] == "ops" and decoded["dst"]["request_id"] == "req"


def test_the_operator_context_has_no_groups() -> None:
    ctx = CubeContext.operator()
    assert ctx.groups == [] and ctx.caller == "dst" and ctx.request_id


def test_a_wrong_or_missing_secret_is_cube_down_not_a_hidden_member() -> None:
    for secret in ("not-the-secret-not-the-secret-not", None):
        with pytest.raises(CubeError) as exc:
            client(FakeCube(), secret=secret).meta(CTX)
        assert (exc.value.mode, exc.value.provider, exc.value.status) == (
            "provider_down",
            "cube",
            403,
        )


# ── failure modes ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (400, "'nope' not found for path 'orders_view.nope'", ("not_declared", None)),
        (400, "Cube 'nope' not found for path 'nope.count'", ("not_declared", None)),
        (400, "Can't find join path to join 'orders', 'islands'", ("not_expressible", None)),
        (
            400,
            'Invalid query format: "filters[0]" does not match any of the allowed types',
            (None, None),
        ),
        (403, "Invalid token", ("provider_down", "cube")),
        (403, "Authorization header isn't set", ("provider_down", "cube")),
        (403, "You don't have permission to access orders.salary", ("hidden_column", None)),
        (
            500,
            "Error: Compile errors: Found conflicting cube and view name 'customers'.",
            ("provider_down", "cube"),
        ),
        (500, "Error: connect ECONNREFUSED 10.0.0.5:5432", ("provider_down", "warehouse")),
        (500, 'Error: relation "orders" does not exist', ("provider_down", "warehouse")),
        (502, "Bad Gateway", ("provider_down", "cube")),
        (None, "ConnectError: connection refused", ("provider_down", "cube")),
    ],
)
def test_every_cube_answer_maps_to_one_failure_mode(
    status: int | None, detail: str, expected: tuple[str | None, str | None]
) -> None:
    got = failure_mode(status, detail)
    assert got == expected
    # The vocabulary's own names, never free strings.
    assert got[0] is None or isinstance(got[0], FailureMode)


def test_a_member_cube_does_not_know_is_not_declared_on_sql_and_load() -> None:
    c = client(FakeCube())
    for call in (c.sql, c.load):
        with pytest.raises(CubeError) as exc:
            call(CubeQuery(measures=["orders_view.nope"]), CTX)
        assert exc.value.mode == "not_declared" and exc.value.status == 400


def test_a_shape_cube_cannot_join_is_not_expressible() -> None:
    with pytest.raises(CubeError) as exc:
        client(FakeCube()).sql(CubeQuery(measures=["orders.count", "islands.count"]), CTX)
    assert exc.value.mode == "not_expressible"


def test_a_query_format_error_is_a_fault_not_a_mode() -> None:
    bad = CubeQuery(
        measures=["orders.count"],
        filters=[CubeFilter(member="orders.status", operator="wat", values=["x"])],
    )
    with pytest.raises(CubeError) as exc:
        client(FakeCube()).sql(bad, CTX)
    assert exc.value.mode is None and exc.value.status == 400
    assert str(exc.value).startswith("error: Invalid query format")


# ── the token's clock ─────────────────────────────────────────────────────


def _wall() -> float:
    hatch = time_machine.escape_hatch
    return hatch.time.time() if hatch.is_travelling() else time.time()


def test_a_pinned_business_clock_never_mints_a_token_cube_finds_expired() -> None:
    # dst's clock pinned to the asked-on day, as tests and the bench pin it;
    # Cube checks expiry on its own (real) clock.
    fake = FakeCube()
    with time_machine.travel(datetime(2026, 9, 1, 9, tzinfo=UTC), tick=True):
        client(fake).meta(CTX)
    (claims,) = fake.claims
    assert claims["iat"] <= _wall() < claims["exp"]


def test_the_process_clock_is_kept_when_asked_for() -> None:
    fake = FakeCube()
    pinned = datetime(2026, 9, 1, 9, tzinfo=UTC)
    with time_machine.travel(pinned, tick=False):
        with pytest.raises(CubeError, match="Invalid token"):
            CubeClient(
                "http://cube:4000/", SECRET, transport=fake.transport(), jwt_clock="process"
            ).meta(CTX)
    sent = fake.requests[-1].headers["authorization"]
    claims = jwt.decode(sent, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    # Dated by the pinned clock, so a Cube on the real clock finds it expired.
    assert claims["iat"] == int(pinned.timestamp())


def test_jwt_clock_is_read_from_dst_yaml_and_the_env_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import settings
    from services.cube.client import configured_from
    from services.project.schema import CubeDecl

    decl = CubeDecl(url="http://cube:4000", jwt_clock="process")
    assert configured_from(decl).jwt_clock == "process"  # type: ignore[union-attr]
    assert configured_from(CubeDecl(url="http://cube:4000")).jwt_clock == "wall"  # type: ignore[union-attr]
    monkeypatch.setattr(settings, "cube_jwt_clock", "wall")
    assert configured_from(decl).jwt_clock == "wall"  # type: ignore[union-attr]
