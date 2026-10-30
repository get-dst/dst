"""The Cube REST client: meta, sql, dry-run, load, readiness.

One httpx client over ``/cubejs-api/v1``, every call made as a ``CubeContext``
(the per-request JWT). ``meta`` is cached per context and ``cube.meta_ttl``
seconds, since a caller's groups decide what Cube shows. ``load`` polls through
Cube's ``Continue wait`` under the serving bound, so a query Cube never finishes
ends the request naming Cube, never a request that never returns.

Every failure is a ``CubeError`` carrying the failure mode the caller reports:
a member Cube does not know is ``not_declared``, a shape Cube cannot join is
``not_expressible``, a refusal under an access policy is ``hidden_column``, Cube
itself unreachable or erroring is ``provider_down`` naming ``cube``, and a
warehouse error Cube relays is ``provider_down`` naming ``warehouse``. An error
that fits none of those is a fault (``mode`` None): it fails loud.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from services.config import resolve_env_ref, settings
from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_query import CubeQuery
from services.contracts.failure_modes import FailureMode
from services.cube.context import CubeContext, wall_now
from services.runtime.bounded import Stalled, run_bounded

if TYPE_CHECKING:
    from services.project.schema import CubeDecl

log = logging.getLogger("dst.cube")

API = "/cubejs-api/v1"
CONTINUE_WAIT = "Continue wait"

# What Cube says when the query names a member the model does not hold:
# "'nope' not found for path 'orders.nope'", "Cube 'nope' not found for path …".
_NOT_DECLARED = re.compile(r"not found for (?:path|query)|not found in cube", re.IGNORECASE)
_NOT_EXPRESSIBLE = re.compile(r"can't find join path|cannot find join path", re.IGNORECASE)
# A 403 over the token itself ("Invalid token", "Authorization header isn't
# set") is dst misconfigured against Cube, not a member withheld from a caller.
_CREDENTIALS = re.compile(r"token|authorization|secret", re.IGNORECASE)
# A 5xx whose text is the warehouse talking through Cube, not Cube itself.
_WAREHOUSE = re.compile(
    r"ECONNREFUSED|ETIMEDOUT|ENOTFOUND|connection (?:refused|terminated|timed out)|"
    r"syntax error|does not exist|permission denied|database|relation|catalog|"
    r"sql compilation|sqlstate|password authentication",
    re.IGNORECASE,
)


class CubeError(RuntimeError):
    """A Cube call that did not produce what was asked. ``mode`` is the failure
    mode the caller reports (None = a fault, status error); ``provider`` names
    ``cube`` or ``warehouse`` on ``provider_down``."""

    def __init__(
        self,
        detail: str,
        *,
        mode: FailureMode | None,
        provider: str | None = None,
        status: int | None = None,
    ) -> None:
        self.detail = detail
        self.mode = mode
        self.provider = provider
        self.status = status
        where = f" ({provider})" if provider else ""
        super().__init__(f"{mode or 'error'}{where}: {detail}")


def failure_mode(status: int | None, detail: str) -> tuple[FailureMode | None, str | None]:
    """``(mode, provider)`` for a Cube response: the mapping, in one place, so
    a test can pin every row. ``status`` None is a transport failure. A 400
    Cube explains as a query-format error is a fault (dst sent a bad body),
    not a mode."""
    if status is None or status >= 500:
        if status is not None and _WAREHOUSE.search(detail):
            return FailureMode.provider_down, "warehouse"
        return FailureMode.provider_down, "cube"
    if status in (401, 403):
        if _CREDENTIALS.search(detail):
            return FailureMode.provider_down, "cube"
        return FailureMode.hidden_column, None
    if status == 400:
        if _NOT_EXPRESSIBLE.search(detail):
            return FailureMode.not_expressible, None
        if _NOT_DECLARED.search(detail):
            return FailureMode.not_declared, None
    return None, None


@dataclass(frozen=True)
class CubeConfig:
    url: str
    api_secret: str | None
    meta_ttl: int = 300
    jwt_clock: Literal["wall", "process"] = "wall"


@dataclass(frozen=True)
class CubeStatus:
    reachable: bool
    url: str
    version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class CompiledSql:
    """What ``/v1/sql`` returns: the SQL text Cube would run, its parameters,
    and the whole payload for anyone who needs the rest (pre-aggregations)."""

    sql: str
    params: list[Any]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadResult:
    data: list[dict[str, Any]]
    annotation: dict[str, Any]
    query: dict[str, Any]
    last_refresh_time: str | None = None
    db_type: str | None = None


def configured(root: Path | None = None) -> CubeConfig | None:
    """The Cube this install talks to, or None. The env (``DST_CUBE_URL``,
    ``DST_CUBE_API_SECRET_ENV``, ``DST_CUBE_META_TTL``) wins field by field;
    ``cube:`` in dst.yaml fills what the env leaves. The secret itself is
    resolved from the named variable, never held in a file. ``root`` is a
    ``--dir`` project: its dst.yaml and .env, not the server's."""
    decl = None
    if root is not None:
        from services.project.schema import parse_project_yaml

        path = root / "dst.yaml"
        if path.exists():
            decl = parse_project_yaml(path.read_text(encoding="utf-8")).cube
    else:
        from services.project.source import project_config

        proj = project_config()
        decl = proj.cube if proj is not None else None
    return configured_from(decl, root=root)


def configured_from(decl: CubeDecl | None, *, root: Path | None = None) -> CubeConfig | None:
    """``configured`` over a ``cube:`` block already in hand (a pushed
    dst.yaml's, at plan and apply): the env still wins field by field."""
    url = settings.cube_url or (decl.url if decl else None)
    if not url:
        return None
    secret_env = (
        settings.cube_api_secret_env
        or (decl.api_secret_env if decl else None)
        or "CUBEJS_API_SECRET"
    )
    ttl = settings.cube_meta_ttl
    if ttl is None:
        ttl = decl.meta_ttl if decl else 300
    return CubeConfig(
        url=url.rstrip("/"),
        api_secret=resolve_env_ref(secret_env, dirs=(root,) if root is not None else None),
        meta_ttl=ttl,
        jwt_clock=settings.cube_jwt_clock or (decl.jwt_clock if decl else "wall"),
    )


class CubeClient:
    def __init__(
        self,
        url: str,
        api_secret: str | None,
        *,
        meta_ttl: int = 300,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        jwt_clock: Literal["wall", "process"] = "wall",
    ) -> None:
        self.url = url.rstrip("/")
        self.api_secret = api_secret
        self.meta_ttl = meta_ttl
        self.jwt_clock = jwt_clock
        self._http = httpx.Client(base_url=self.url, timeout=timeout, transport=transport)
        self._meta_cache: dict[tuple[Any, ...], tuple[float, MetaDoc]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: CubeConfig, **kwargs: Any) -> CubeClient:
        return cls(
            cfg.url, cfg.api_secret, meta_ttl=cfg.meta_ttl, jwt_clock=cfg.jwt_clock, **kwargs
        )

    def close(self) -> None:
        self._http.close()

    # ── calls ───────────────────────────────────────────────────────────

    def ready(self) -> CubeStatus:
        """``/readyz``: reachable and healthy, plus the version when the body
        or a header says it (Cube's health body does not by itself)."""
        try:
            response = self._http.get("/readyz")
        except httpx.HTTPError as exc:
            return CubeStatus(False, self.url, detail=f"{type(exc).__name__}: {exc}")
        body: dict[str, Any] = {}
        try:
            body = response.json() if response.content else {}
        except ValueError:
            pass
        version = body.get("version") or response.headers.get("x-cube-version")
        healthy = response.status_code == 200 and body.get("health", "HEALTH") == "HEALTH"
        return CubeStatus(
            healthy,
            self.url,
            version=str(version) if version else None,
            detail="" if healthy else f"HTTP {response.status_code}: {response.text[:200]}",
        )

    def meta(self, ctx: CubeContext, *, extended: bool = False) -> MetaDoc:
        """``GET /v1/meta`` as this context, cached ``meta_ttl`` seconds per
        (org, groups, extended): what a caller may see is the context's."""
        key = (ctx.org, tuple(ctx.groups), extended)
        now = time.monotonic()
        with self._lock:
            hit = self._meta_cache.get(key)
            if hit and now - hit[0] < self.meta_ttl:
                return hit[1]
        payload = self._call("GET", "/meta", ctx, params={"extended": "true"} if extended else None)
        doc = MetaDoc.parse(payload)
        with self._lock:
            self._meta_cache[key] = (now, doc)
        return doc

    def forget_meta(self) -> None:
        with self._lock:
            self._meta_cache.clear()

    def sql(self, query: CubeQuery, ctx: CubeContext) -> CompiledSql:
        """``POST /v1/sql``: the SQL Cube would run for this query."""
        payload = self._call("POST", "/sql", ctx, json={"query": query.wire()})
        compiled = payload.get("sql") or {}
        pair = compiled.get("sql") or ["", []]
        return CompiledSql(
            sql=str(pair[0]), params=list(pair[1]) if len(pair) > 1 else [], raw=payload
        )

    def dry_run(self, query: CubeQuery, ctx: CubeContext) -> dict[str, Any]:
        """``POST /v1/dry-run``: Cube's normalised reading of the query, no
        warehouse touched."""
        return self._call("POST", "/dry-run", ctx, json={"query": query.wire()})

    def load(
        self, query: CubeQuery, ctx: CubeContext, *, deadline: float | None = None
    ) -> LoadResult:
        """``POST /v1/load``, polling through ``Continue wait`` until rows come
        or ``deadline`` seconds pass (default: the serving timeout). Past it,
        ``provider_down`` naming Cube: a query it never finishes is Cube not
        answering, and the request ends saying so."""
        seconds = settings.serving_timeout_s if deadline is None else deadline
        body: dict[str, Any] = {"query": query.wire()}
        if any(t.compareDateRange for t in query.timeDimensions):
            # Cube answers a compareDateRange ask only to a client that says it
            # can read several result sets; the rows come back one set per
            # window, each row tagged with its window.
            body["queryType"] = "multi"

        def poll() -> dict[str, Any]:
            started = time.monotonic()
            while True:
                payload = self._call("POST", "/load", ctx, json=body)
                if payload.get("error") != CONTINUE_WAIT:
                    return payload
                if seconds > 0 and time.monotonic() - started > seconds:
                    raise CubeError(
                        f"Cube kept answering '{CONTINUE_WAIT}' for {seconds:g}s",
                        mode=FailureMode.provider_down,
                        provider="cube",
                    )
                time.sleep(min(0.5, max(seconds, 0.05)))

        try:
            payload = poll() if seconds <= 0 else run_bounded("dst-cube-load", poll, seconds)
        except Stalled:
            raise CubeError(
                f"Cube did not return within {seconds:g}s (DST_SERVING_TIMEOUT_S)",
                mode=FailureMode.provider_down,
                provider="cube",
            ) from None
        results = payload.get("results")
        if isinstance(results, list) and results:
            # compareDateRange: one result set per window; the rows carry their
            # window under ``compareDateRange`` already.
            return LoadResult(
                data=[row for r in results for row in r.get("data") or []],
                annotation=dict(results[0].get("annotation") or {}),
                query=dict(results[0].get("query") or {}),
                last_refresh_time=results[0].get("lastRefreshTime"),
                db_type=results[0].get("dbType"),
            )
        return LoadResult(
            data=list(payload.get("data") or []),
            annotation=dict(payload.get("annotation") or {}),
            query=dict(payload.get("query") or {}),
            last_refresh_time=payload.get("lastRefreshTime"),
            db_type=payload.get("dbType"),
        )

    # ── transport ───────────────────────────────────────────────────────

    def _headers(self, ctx: CubeContext) -> dict[str, str]:
        if not self.api_secret:
            return {}
        now = wall_now() if self.jwt_clock == "wall" else None
        return {"Authorization": ctx.token(self.api_secret, now=now)}

    def _call(
        self,
        method: str,
        path: str,
        ctx: CubeContext,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(
                method, API + path, json=json, params=params, headers=self._headers(ctx)
            )
        except httpx.HTTPError as exc:
            raise CubeError(
                f"{type(exc).__name__}: {exc}", mode=FailureMode.provider_down, provider="cube"
            ) from exc
        payload = _json(response)
        if response.status_code == 200:
            error = payload.get("error")
            if error and error != CONTINUE_WAIT:
                mode, provider = failure_mode(200, str(error))
                raise CubeError(str(error), mode=mode, provider=provider, status=200)
            return payload
        detail = str(payload.get("error") or response.text[:500] or f"HTTP {response.status_code}")
        mode, provider = failure_mode(response.status_code, detail)
        raise CubeError(detail, mode=mode, provider=provider, status=response.status_code)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json() if response.content else {}
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {"data": payload}


# ── status for /ready, doctor and `dst cube status` ──────────────────────


def status(
    lens_views: list[str] | None = None, *, root: Path | None = None
) -> dict[str, Any] | str:
    """What the operator surfaces show: ``"not configured"``, or the URL, the
    readiness, the version, the model_version and the member counts. Never a
    gate; never raises."""
    cfg = configured(root)
    if cfg is None:
        return "not configured"
    client = CubeClient.from_config(cfg, timeout=5.0)
    try:
        ready = client.ready()
        out: dict[str, Any] = {
            "url": cfg.url,
            "reachable": ready.reachable,
            "version": ready.version,
            "secret": "set" if cfg.api_secret else "unset",
        }
        if not ready.reachable:
            out["detail"] = ready.detail
            return out
        try:
            doc = client.meta(CubeContext.operator())
        except CubeError as exc:
            out["detail"] = exc.detail
            return out
        out["model_version"] = doc.model_version(lens_views)
        out["members"] = doc.member_counts(lens_views)
        out["overlay_errors"] = len(doc.overlay_errors)
        return out
    finally:
        client.close()
