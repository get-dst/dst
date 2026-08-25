"""FastAPI application entry point.

Health/observability from day 0: `/health` (liveness) and `/ready` (dependencies),
structured JSON logging, and a catch-all error handler. Routers are mounted here
(connections, lenses, query, mcp, observe, reviews).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from services import __version__ as services_version
from services.api.auth_local import router as auth_local_router
from services.api.auth_local import users_router as mgmt_users_router
from services.api.mgmt import router as mgmt_router
from services.api.mgmt_activation import router as mgmt_activation_router
from services.api.mgmt_audit import router as mgmt_audit_router
from services.api.mgmt_callers import router as mgmt_callers_router
from services.api.mgmt_catalog import router as mgmt_catalog_router
from services.api.mgmt_certify import router as mgmt_certify_router
from services.api.mgmt_connections import router as mgmt_connections_router
from services.api.mgmt_distill import router as mgmt_distill_router
from services.api.mgmt_evals import router as mgmt_evals_router
from services.api.mgmt_gap_map import router as mgmt_gap_map_router
from services.api.mgmt_lenses import router as mgmt_lenses_router
from services.api.mgmt_observe import router as mgmt_observe_router
from services.api.mgmt_profile import connection_router as profile_connection_router
from services.api.mgmt_profile import lens_drift_router as profile_lens_drift_router
from services.api.mgmt_profile import lens_profile_router as profile_lens_profile_router
from services.api.mgmt_profile import router as mgmt_profile_router
from services.api.mgmt_project import router as mgmt_project_router
from services.api.mgmt_semantic import router as mgmt_semantic_router
from services.api.mgmt_standards import router as mgmt_standards_router
from services.api.oauth import router as oauth_router
from services.api.openai_compat import router as openai_compat_router
from services.api.query import router as query_router
from services.api.receipts import router as receipts_router
from services.api.reviews import data_router as reviews_data_router
from services.api.reviews import mgmt_router as reviews_mgmt_router
from services.api.reviews import patches_router as reviews_patches_router
from services.api.route import router as route_router
from services.api.security_headers import SecurityHeaders
from services.api.sql import router as sql_router
from services.api.surface import router as surface_router
from services.auth.deps import resolve_mcp_caller
from services.auth.tokens import ADMIN_PREFIX
from services.build_info import GIT_DIRTY, GIT_SHA
from services.config import settings, validate_production_contract
from services.context import store as ctx_serving_store
from services.contracts.errors import ProviderError
from services.db import embedding_meta
from services.db.session import engine
from services.logging_config import configure_logging
from services.mcp.server import mcp as mcp_server
from services.observability import logger as trace_logger
from services.plugins import load_plugins
from services.plugins import status as plugin_status

configure_logging(settings.log_level)
log = logging.getLogger("dst")

# Serving in production requires the full env contract; fail at import, not first request.
validate_production_contract()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP session manager for the app's lifetime."""
    # The other half of the startup contract, and the half that needs a database:
    # a DST_SECRET_KEY that cannot decrypt this install's sentinel means every
    # stored warehouse credential is unreadable. Fail here, not on whichever
    # request first touches a connector. Import-time is too early — the DB may not
    # be up yet, and `dst migrate` must still run against a fresh database.
    from services.security.sentinel import verify_or_install

    await run_in_threadpool(verify_or_install)

    async with mcp_server.session_manager.run():
        yield


# One description per tag — /docs is the reference of record for the control plane,
# so a tag without one renders as a bare label (the docs-parity test pins these).
_openapi_tags = [
    {"name": "meta", "description": "Liveness and readiness — no auth."},
    {"name": "mgmt", "description": "Admin-token sanity: ping + whoami."},
    {"name": "auth", "description": "Local dashboard sessions (`dstsess_`) — login, logout, me."},
    {"name": "users", "description": "Local dashboard users (admin)."},
    {
        "name": "lenses",
        "description": "Lens lifecycle — drafts, validate/publish, versions, repo tree + "
        "diffs, drift.",
    },
    {"name": "callers", "description": "Caller identities and their `dst_` API keys (admin)."},
    {"name": "connections", "description": "Warehouse connections — CRUD, probe, dependents."},
    {
        "name": "audit",
        "description": "The per-connection audit — mined query history, accuracy, "
        "governed coverage, drift findings.",
    },
    {
        "name": "activation",
        "description": "The org's activation step, derived from persisted state (no writes).",
    },
    {
        "name": "certified",
        "description": "The per-lens certified corpus — list, certify directly, generate, "
        "promote from served requests.",
    },
    {
        "name": "receipts",
        "description": "Verify an answer receipt — signature + cross-check against the "
        "logged trace.",
    },
    {
        "name": "catalog",
        "description": "Wizard pickers — available warehouse types + a connection's tables.",
    },
    {
        "name": "observe",
        "description": "Read-only ops: KPIs, per-caller rollups, request traces, eval trend.",
    },
    {
        "name": "profile",
        "description": "Stored table profiles, join candidates, and profile drift.",
    },
    {"name": "evals", "description": "Per-lens eval cases and recorded runs; the distiller."},
    {
        "name": "standards",
        "description": "Org-standard definitions — the baseline drift checks compare against.",
    },
    {
        "name": "semantic",
        "description": "The shared semantic layer — entity/definition assets + introspection.",
    },
    {
        "name": "reviews",
        "description": "The correction loop — tickets, rulings, drafted patches. The `/v1` "
        "half is caller-scoped; `/mgmt` is the whole queue.",
    },
    {
        "name": "query",
        "description": "The data plane — ask a lens, structured metrics, certified runs, "
        "guarded SQL, governed definitions.",
    },
    {"name": "surface", "description": "The router-lens's surface area for the org."},
    {"name": "gap-map", "description": "The warehouse coverage decomposition per connection."},
    {"name": "project", "description": "File-first deployment — export, plan, apply."},
    {"name": "openai", "description": "OpenAI-compatible chat completions over lenses."},
    {"name": "oauth", "description": "PKCE authorization-server facade for MCP clients."},
]

app = FastAPI(
    title="dst",
    version=services_version,
    lifespan=lifespan,
    description=(
        "Three surfaces on one port: the **data plane** (`/v1`, caller keys) for asking, "
        "the **control plane** (`/mgmt`, admin tokens) for managing the install, and the "
        "governed **MCP door** (`/mcp`). Auth is `Authorization: Bearer <token>` everywhere: "
        "`dstadm_` admin tokens on `/mgmt`, `dst_` caller keys on `/v1` and `/mcp`."
    ),
    openapi_tags=_openapi_tags,
)


@app.exception_handler(ProviderError)
async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
    # Upstream model provider failed — the caller's request is fine; 502, not 500.
    log.warning("provider error: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"upstream model provider failed — {exc}; retry shortly"},
    )


@app.exception_handler(OperationalError)
async def _db_unreachable(request: Request, exc: OperationalError) -> JSONResponse:
    # The state DB vanished mid-flight (container stopped, port moved). A raw
    # 500 with a SQLAlchemy traceback taught nobody anything;
    # name the disease and the check. /health stays liveness-only; /ready is
    # the probe that includes the DB.
    log.warning("database unreachable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "database unreachable — is the project's Postgres running? "
            "(docker compose ps; DATABASE_URL in .env must match its port). "
            "Server liveness is /health; DB-inclusive readiness is /ready."
        },
    )


# Dev SPA origins stay out of production CORS — same-origin serving needs none,
# split deploys declare theirs via DST_CORS_ORIGINS.
_cors_origins = (
    []
    if settings.environment == "production"
    else ["http://localhost:5173", "http://localhost:3000"]
)
if settings.cors_origins:
    _cors_origins += [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(mgmt_router)
# Local (Clerk-free) dashboard login + user management — the self-host path.
app.include_router(auth_local_router)
app.include_router(mgmt_users_router)
app.include_router(mgmt_lenses_router)
app.include_router(mgmt_callers_router)
app.include_router(mgmt_connections_router)
app.include_router(mgmt_audit_router)
app.include_router(mgmt_activation_router)
app.include_router(mgmt_certify_router)
app.include_router(mgmt_catalog_router)
app.include_router(mgmt_observe_router)
app.include_router(mgmt_profile_router)
app.include_router(profile_connection_router)
app.include_router(profile_lens_drift_router)
app.include_router(profile_lens_profile_router)
app.include_router(mgmt_evals_router)
app.include_router(mgmt_distill_router)
app.include_router(mgmt_standards_router)
app.include_router(mgmt_semantic_router)
app.include_router(reviews_data_router)
app.include_router(reviews_mgmt_router)
app.include_router(reviews_patches_router)
app.include_router(query_router)
app.include_router(receipts_router)
app.include_router(sql_router)
app.include_router(route_router)
app.include_router(surface_router)
app.include_router(mgmt_gap_map_router)
app.include_router(mgmt_project_router)
app.include_router(openai_compat_router)
# OAuth AS facade for MCP (root-mounted: RFC 9728 metadata lives at the host root).
app.include_router(oauth_router)


def _base_url(scope: Scope, headers: dict[str, str]) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    proto = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
    return f"{proto}://{headers.get('host', 'localhost')}"


async def _send_json(
    send: Send,
    status: int,
    body: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), *(extra_headers or [])]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


class _McpGate:
    """Outer ASGI middleware for the MCP surface.

    Intercepts ``/mcp`` (with or without a trailing slash) *before* FastAPI routing, for
    two reasons: (1) ``POST /mcp`` answers directly instead of Starlette's mount issuing a
    307 to ``/mcp/`` (raw POST clients don't follow it); (2) the bearer is validated before
    JSON-RPC, so a bad/missing/expired key is a client-visible auth failure at connect time
    rather than a working-looking session that fails on every tool call. Missing/invalid →
    401 + ``WWW-Authenticate`` pointing at the protected-resource metadata (the hook a
    client's native OAuth flow hangs on); ``dstadm_`` → 403 with remediation. In-session
    revocation still surfaces in the tool envelope (the data plane re-checks per call)."""

    def __init__(self, app: Any, mcp_app: Any) -> None:
        self._app = app
        self._mcp = mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_mcp_path(scope["path"]):
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        authz = headers.get("authorization", "")
        token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
        base = _base_url(scope, headers)
        resource_meta = f"{base}/.well-known/oauth-protected-resource/mcp"
        challenge = (b"www-authenticate", f'Bearer resource_metadata="{resource_meta}"'.encode())

        if not token:
            await _send_json(
                send,
                401,
                {
                    "error": "unauthorized",
                    "auth_url": f"{base}/.well-known/oauth-authorization-server",
                    "hint": "Connect via OAuth (no header needed) or send "
                    "'Authorization: Bearer dst_…'.",
                },
                [challenge],
            )
            return
        if token.startswith(ADMIN_PREFIX):
            await _send_json(
                send,
                403,
                {
                    "error": "admin_token_forbidden",
                    "hint": "Admin tokens can't be used over MCP. Issue a scoped caller key "
                    "in Settings → Callers, or connect via OAuth.",
                },
            )
            return
        # The audience this deployment answers to (RFC 8707) — the same string the
        # protected-resource metadata publishes as `resource`, so a token minted
        # against a different base URL is refused here rather than honoured.
        if (
            await run_in_threadpool(partial(resolve_mcp_caller, resource=f"{base}/mcp"), token)
        ) is None:
            await _send_json(
                send,
                401,
                {
                    "error": "invalid_token",
                    "auth_url": f"{base}/.well-known/oauth-authorization-server",
                    "hint": "Key invalid, expired, revoked, or issued for a different "
                    "dst deployment — re-authenticate or issue a new key.",
                },
                [challenge],
            )
            return
        # Strip the /mcp prefix ourselves ("/mcp" and "/mcp/" both → "/") and hand to the
        # streamable-HTTP app, whose route is "/". Doing this here (not via app.mount) is
        # what avoids the 307.
        inner = scope["path"][len("/mcp") :] or "/"
        scope = {**scope, "path": inner, "raw_path": inner.encode("latin-1")}
        await self._mcp(scope, receive, send)


# Remote MCP (streamable-HTTP). Intercepted at the ASGI edge by _McpGate (transport auth +
# no 307); tools still read the per-request bearer key (see mcp/server.py). The unwrapped
# inner app is reused by the /ready liveness probe (no auth, in-process).
_mcp_http_app = mcp_server.streamable_http_app()
app.add_middleware(_McpGate, mcp_app=_mcp_http_app)

# Added last, so it wraps everything else and no surface can answer without the
# response security headers — including the ones _McpGate answers itself.
app.add_middleware(SecurityHeaders)


async def _mcp_alive() -> bool:
    """Self JSON-RPC `initialize` round-trip against the in-process MCP app (no network,
    no auth gate). Catches a wedged session manager — seen under long-lived --reload —
    turning an infinite client hang into a visible `degraded`."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "readyz", "version": "0"},
        },
    }
    try:
        # base_url host must be one the MCP transport's DNS-rebinding
        # protection accepts. Its allowlist matches exact entries or
        # "host:*" port-wildcards — which REQUIRE a port, so bare
        # "localhost" 421s exactly like "mcp" would.
        # Any port works (the ASGI transport never binds one) — port 0,
        # because a plausible one lies: ":8000" in this probe's log line
        # reads exactly like a real dead-port MCP misconfiguration.
        transport = httpx.ASGITransport(app=_mcp_http_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:0") as client:
            r = await client.post(
                "/",
                json=body,
                headers={"accept": "application/json, text/event-stream"},
                timeout=2.0,
            )
        return r.status_code < 500
    except Exception:
        log.warning("MCP liveness probe failed", exc_info=True)
        return False


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str | bool | None]:
    """Liveness — process is up. `edition` is UI badging only, never a feature gate.
    git_sha/git_dirty name the running BUILD (the pyproject version holds
    still across fixes, so a stale process masqueraded as current code); null on
    packaged installs, captured once at startup."""
    return {
        "status": "ok",
        "edition": settings.edition,
        "version": services_version,
        "git_sha": GIT_SHA,
        "git_dirty": GIT_DIRTY,
    }


def _embedding_status() -> str:
    """Configured embedder vs stored embedding_meta. Informational only — a mismatch
    blocks embedding *writes* (guard) but reads still serve, so it never degrades
    readiness; it surfaces here + as a warning log so the operator sees the fix."""
    try:
        from services.llm import registry

        try:
            embedder = registry.resolve_embedder()
        except Exception as exc:
            # The embedder is CONFIGURED and cannot load (reaped model cache, no
            # network, bad credentials). This used to fall through to the blanket
            # handler below and report "unknown" — the operator's one health
            # string said nothing at all while certified matching was dead for
            # the whole process. Name it, and name the cause.
            log.error("embedder unavailable — certified matching cannot fire: %s", exc)
            return f"unavailable ({exc})"
        if embedder is None:
            return "unconfigured"
        live_error = ctx_serving_store.LAST_SERVING_ERROR.get("error")
        if live_error:
            return f"failing ({live_error})"
        model, dim = embedding_meta.identity(embedder)
        with engine.connect() as conn:
            stored = embedding_meta.read_meta(conn)
            # Meta agreeing (or empty) isn't enough — on a fresh install the
            # PHYSICAL columns can still disagree with the configured embedder
            # (vector(1024) columns vs the local tier's 384).
            column_mismatch = any(
                embedding_meta.column_dim(conn, t) != dim for t in embedding_meta._VECTOR_TABLES
            )
        if column_mismatch:
            log.warning(
                "embedding columns don't match the configured embedder %s (dim %s) — "
                "embedding writes are blocked until `dst reindex` (or re-run "
                "`dst migrate` on an empty install)",
                model,
                dim,
            )
            return "reindex-needed"
        if stored is None or stored == (model, dim):
            return "ok"
        log.warning(
            "embedding config mismatch: stored vectors are %s (dim %s) but the configured "
            "embedder is %s (dim %s) — embedding writes are blocked until `dst reindex`",
            stored[0],
            stored[1],
            model,
            dim,
        )
        return "reindex-needed"
    except Exception:  # pre-migration DB etc. — readiness must never crash on this
        return "unknown"


def _models_status() -> str:
    """What this install would actually call: tier → provider/model, plus the
    embedder (services/llm/registry.py::serving_summary). Informational, never a
    readiness gate — a keyless install still serves certified answers and the
    dashboard. It is here because "which model does my lens run on?" had no
    answer anywhere: a lens defaulting to a vendor nobody configured published
    green and 503'd on every question, and nothing said so until a user asked."""
    try:
        from services.llm import registry

        return registry.serving_summary()
    except Exception:  # readiness must never crash on a config read
        return "unknown"


def _matching_status() -> str:
    """Can the certified door fire AT ALL? "ok" or "unavailable".

    Certified matching is pgvector cosine over a question vector, so no embedder
    means no match is possible for any question, however exactly it is asked —
    the product's fastest and most deterministic path, off. `embeddings` above
    already carries the diagnosis, but it is a sentence: an operator watching one
    field for "is the governed door open?" should not have to parse English to
    find out, and a silently-off certified door looks exactly like an ordinary
    session that generates every answer."""
    from services.llm import registry

    try:
        return "ok" if registry.resolve_embedder() is not None else "unavailable"
    except Exception:  # already logged, with the cause, by _embedding_status
        return "unavailable"


def _schema_status() -> tuple[str, bool]:
    """(one-line schema state, is-it-a-gate). `SELECT 1` answered "the database is
    reachable" and was read as "the database is right" — a schema two revisions behind
    reported `db: ok, status: ready` while every trace write failed (see
    services/db/schema_state.py). Only `behind` degrades readiness: `ahead` is the safe
    deploy order and `unknown` is a question that could not be asked."""
    from services.db.schema_state import schema_state

    state = schema_state()
    return state.summary(), state.status == "behind"


@app.get("/ready", tags=["meta"])
async def ready() -> dict[str, str]:
    """Readiness — dependencies reachable (DB + the MCP session manager), the schema
    caught up with this build, and the audit trail actually landing.

    `certified_matching` is deliberately NOT a readiness gate: an install with no
    embedder still serves generated answers and the dashboard, and gating on it
    would take those installs down. It is reported so a degraded one is visible."""

    def _db_check() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        await run_in_threadpool(_db_check)
        db = "ok"
    except Exception:
        log.exception("readiness DB check failed")
        db = "down"
    mcp = "ok" if await _mcp_alive() else "down"
    # Threadpooled: pooled-DB I/O, and first use of the local embedding tier
    # constructs the model — a slow readiness probe must never stall the loop.
    embeddings = await run_in_threadpool(_embedding_status)
    matching = await run_in_threadpool(_matching_status)
    models = await run_in_threadpool(_models_status)
    schema, schema_blocks = await run_in_threadpool(_schema_status)
    traces = trace_logger.trace_write_status()
    return {
        "status": (
            "ready"
            if db == "ok" and mcp == "ok" and not schema_blocks and traces == "ok"
            else "degraded"
        ),
        "db": db,
        "schema": schema,
        # Whether the receipts are landing. A background-task INSERT that fails is
        # invisible to the caller by construction — the response is already sent — so
        # the health surface is where it has to show up.
        "traces": traces,
        "mcp": mcp,
        "embeddings": embeddings,
        "certified_matching": matching,
        "models": models,
        # What an entry-point plugin added to this install's route table. Never a
        # readiness gate — a plugin is an extension, not a dependency — but an
        # operator must be able to see that `pip install` changed what the API serves.
        "plugins": plugin_status(),
        "environment": settings.environment,
    }


@app.exception_handler(embedding_meta.EmbeddingMismatchError)
async def embedding_mismatch_handler(
    request: Request, exc: embedding_meta.EmbeddingMismatchError
) -> JSONResponse:
    """The write-path guard's mismatch is an operator config problem, not a bug —
    surface the actionable message (`dst reindex`) instead of a blind 500."""
    return JSONResponse(
        status_code=409,
        content={"error": {"code": "embedding_mismatch", "message": str(exc)}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Internal server error"}},
    )


# Top-level path segments that belong to the API, never to client-side routing.
_API_PREFIXES = frozenset({"v1", "mgmt", "mcp", "health", "ready", "docs", "openapi.json"})


def _mount_spa() -> None:
    """Serve the built React SPA same-origin when DST_WEB_DIST points to a build.

    Registered last so API routers take precedence — but only for paths they
    actually registered; `_API_PREFIXES` keeps the rest of their namespace out of
    the SPA fallback.

    Registered last so API routers (/v1, /mgmt, /health, /docs) take precedence; any
    other GET falls back to index.html for client-side routing. No-op in dev.
    """
    if not settings.web_dist:
        # The wheel ships the built dashboard beside this module, so look there
        # before declaring there isn't one. Without this, anything that runs the
        # app directly — uvicorn, gunicorn, a PaaS process — serves API-only AND
        # logs that the dashboard is absent, while index.html sits in the same
        # package. Only `dst serve` used to find it.
        packaged = Path(__file__).resolve().parent / "web_dist"
        if (packaged / "index.html").is_file():
            settings.web_dist = str(packaged)
        else:
            # Said out loud at startup: the operator otherwise discovers an
            # API-only deploy as a bare 404 at /.
            log.info("dashboard not bundled — serving the API only (set DST_WEB_DIST to a build)")
            return
    dist = Path(settings.web_dist).resolve()
    index = dist / "index.html"
    if not index.is_file():
        log.warning("web_dist=%s has no index.html; not serving SPA", dist)
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # An UNREGISTERED api path must 404, not fall through to index.html.
        # A client pointed at a build without its endpoint would otherwise get
        # `200 text/html` and die in r.json() with "Expecting value: line 1
        # column 1" — an opaque parse error that reads as a dst bug instead of
        # a wrong-path one. Only bundled deploys have the catch-all, so this class
        # is invisible in dev.
        if full_path.partition("/")[0] in _API_PREFIXES:
            raise HTTPException(status_code=404, detail=f"no such endpoint: /{full_path}")
        # `dist / full_path` follows `..` segments out of the build root — a
        # percent-encoded `/%2e%2e/…` reaches here undecoded by the router and
        # would hand back any file the process can read (env, keys). Serve a real
        # file only when it resolves back inside dist; otherwise fall through to
        # index.html exactly as an unknown client route does.
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_relative_to(dist) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    log.info("serving SPA from %s", dist)


# Entry-point plugins (dst.plugins) register after every core router and
# before the SPA catch-all, which would otherwise swallow their GET routes.
load_plugins(app)
_mount_spa()
