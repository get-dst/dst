"""The `dst migrate`, `dst dev`, `dst serve`, `dst mcp` commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import socket

    from services.db.schema_state import SchemaState

_MIGRATE_LOCK_KEY = 0x4B_55_52_4D  # 'KURM'


def _migrate_result(before: SchemaState) -> str:
    """What the upgrade just did, from the state read before it ran.

    `migrated to head` was printed identically whether two revisions had been applied
    or none — so the one user who does the right thing after pulling a new version
    learns nothing, and neither does the one who runs it on an already-current
    database. Pure, so the wording is testable without a database.
    """
    head = before.head or "head"
    if before.status == "ok":
        return f"already at head ({head}) — nothing to apply"
    if before.current is None:
        return f"schema created at {head}"
    if before.status == "behind":
        count = len(before.pending)
        applied = ", ".join(reversed(before.pending))
        return (
            f"migrated {before.current} → {head} — "
            f"{count} revision{'s' if count != 1 else ''} applied ({applied})"
        )
    return f"migrated to {head} (was {before.current})"


def _migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from sqlalchemy import create_engine, text

    from services.config import settings
    from services.db.app_role import sync_app_role_password
    from services.db.schema_state import alembic_config, schema_state

    cfg = alembic_config()
    # Concurrent cold starts must not race the schema: the whole
    # sequence — upgrade, role sync, column sizing — runs under a blocking advisory
    # lock, so waiters apply after the winner and no-op at head.
    lock_engine = create_engine(settings.database_admin_url)
    try:
        with lock_engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATE_LOCK_KEY})
            # Read the state BEFORE upgrading so the line below can say what it did.
            # "migrated to head" printed identically after applying two revisions and
            # after applying none, so a user who did the right thing on an upgrade got
            # no confirmation that anything had been pending.
            before = schema_state()
            command.upgrade(cfg, "head")
            print(_migrate_result(before))
            msg = sync_app_role_password(settings.database_admin_url, settings.database_url)
            if msg:
                print(msg)
            # Fresh-install auto-size: migrations create vector(1024);
            # a configured embedder with another dim (the local tier is 384) would 500
            # the first embedding write. With zero vectors stored, retyping is free —
            # do it here so the zero-config poc tier just works. Identity comes from
            # config alone (no client construction — a local embedder's constructor
            # downloads weights, which migrate must never trigger).
            try:
                from services.db.reindex import size_empty_columns
                from services.llm import registry

                ident = registry.embedding_identity()
                if ident is not None and size_empty_columns(*ident):
                    print(f"embedding columns sized for {ident[0]} (dim {ident[1]})")
            except Exception as exc:  # noqa: BLE001 — sizing is a convenience, never a failure
                print(f"warning: embedding column auto-size skipped: {exc}")
    except Exception as exc:  # noqa: BLE001 — the connection-refused beat: one line, no stack dump
        first = str(exc).splitlines()[0][:200]
        print(
            f"error: migrate failed: {first}\n"
            "If Postgres just started (compose 'healthy' can precede accepting "
            "connections), retry `dst migrate` in a few seconds.",
            file=sys.stderr,
        )
        return 1
    finally:
        lock_engine.dispose()
    return 0


def _wait_for_health(url: str, *, tries: int = 240, delay: float = 0.5) -> bool:
    """True once GET /health answers 200. Startup takes seconds (imports +
    lifespan) after the port is announced — without an explicit ready signal
    every probing script retries blindly."""
    import time

    import httpx

    for _ in range(tries):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(delay)
    return False


def _announce_ready(url: str) -> None:
    """Print the ready line from a side thread — uvicorn owns the main one."""
    import threading

    def poll() -> None:
        if _wait_for_health(url):
            print(f"ready — {url}", flush=True)

    threading.Thread(target=poll, daemon=True).start()


def _probe_socket(host: str) -> socket.socket:
    """A preflight socket configured exactly like the one uvicorn will bind
    (uvicorn/config.py bind_socket): AF_INET6 for a colon-bearing host,
    SO_REUSEADDR set. A probe stricter than the server it guards is a false
    alarm — a bare socket refuses a port still in TIME_WAIT from a just-killed
    server, which is precisely the moment anyone re-runs `dst serve`."""
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family=family)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return probe


def _port_busy_message(port: int, lsof_pids: str) -> str:
    """`lsof -ti` prints one PID per line; interpolating that raw tore the
    message apart mid-sentence. When lsof names nobody, nothing holds the port
    — the bind lost to a lingering socket, not to another server — so say that
    instead of asking about a dst that isn't there."""
    pids = " ".join(lsof_pids.split())
    if not pids:
        return (
            f"error: port {port} would not bind, but lsof sees no process holding it "
            "— the socket is most likely still in TIME_WAIT from a server that just "
            "stopped.\nRetry in a few seconds, or pass --port to use another one."
        )
    return (
        f"error: port {port} is already in use by PID {pids} — another dst running?\n"
        f"Stop it (kill {pids}) or pass --port."
    )


def _mcp(args: argparse.Namespace) -> int:
    """`dst mcp`: the stdio MCP server, exactly as running the module does — the
    wheel has no module path to hand a client config, so the verb is what a
    `claude mcp add` line names. Key and URL ride the module's env semantics
    (DST_API_KEY, DST_URL)."""
    from services.mcp.server import main as run_stdio

    run_stdio()
    return 0


def _serve_state_dir() -> Path:
    """Where a background server leaves its pidfile and log: .dst/ in the
    current project, the per-machine state directory `dst init` gitignores."""
    return Path.cwd() / ".dst"


def _daemon_pid(pidfile: Path) -> int | None:
    """The pid in *pidfile* when that process is still alive, else None (a
    stale file from a server that died is cleared, not obeyed)."""
    import os

    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid
    return pid


def _serve_stop(args: argparse.Namespace) -> int:
    """`dst serve --stop`: SIGTERM the pid in .dst/serve.pid and wait for it."""
    import os
    import signal
    import time

    pidfile = _serve_state_dir() / "serve.pid"
    pid = _daemon_pid(pidfile)
    if pid is None:
        print(f"no background server recorded in {pidfile}", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        print(f"pid {pid} did not exit after SIGTERM; see {pidfile.parent / 'serve.log'}")
        return 1
    pidfile.unlink(missing_ok=True)
    print(f"stopped background server (pid {pid})")
    return 0


_STARTUP_FAILED = "Application startup failed"


def _serve_daemon(args: argparse.Namespace) -> int:
    """`dst serve --daemon`: run the same command detached, pid in
    .dst/serve.pid, output in .dst/serve.log; return once /health answers or
    the child has died (its log tail is the error)."""
    import os
    import subprocess
    import time

    state = _serve_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    pidfile, logfile = state / "serve.pid", state / "serve.log"
    running = _daemon_pid(pidfile)
    if running is not None:
        print(f"a background server is already running (pid {running}); `dst serve --stop` first")
        return 1
    argv = [a for a in sys.argv[1:] if a != "--daemon"]
    with logfile.open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "services.cli.main", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=os.getcwd(),
        )
    pidfile.write_text(f"{child.pid}\n")
    port = args.port or 8000
    url = f"http://{args.host}:{port}"

    def failed(what: str) -> int:
        pidfile.unlink(missing_ok=True)
        tail = logfile.read_text(errors="replace").splitlines()[-20:]
        print(f"background server {what}:", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        return 1

    for _ in range(240):
        if child.poll() is not None:
            return failed(f"exited with {child.returncode}")
        # A startup that failed inside uvicorn does not always end the
        # process: a thread the failed lifespan left behind keeps the
        # interpreter alive, and the pid polled as "running" for ten minutes
        # while the log already held the traceback. The log is the signal.
        if _STARTUP_FAILED in logfile.read_text(errors="replace"):
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
            return failed("failed to start")
        if _wait_for_health(url, tries=1, delay=0):
            break
        time.sleep(0.5)
    else:
        child.terminate()
        return failed("did not answer /health in time")
    print(f"serving in the background — {url} (pid {child.pid}, log {logfile})", flush=True)
    return 0


def _serve(args: argparse.Namespace) -> int:
    import os
    import subprocess
    from urllib.parse import urlparse

    import uvicorn

    from services.config import resolve_env_ref, settings
    from services.db.schema_state import schema_state, serve_refusal

    if getattr(args, "stop", False):
        return _serve_stop(args)
    env_url = resolve_env_ref("DST_URL")
    if args.port is None:
        args.port = (urlparse(env_url).port or 8000) if env_url else 8000
    # The MCP server resolves DST_URL once, at import, to proxy its tools back
    # into this API — and a --port flag exists nowhere it can see, so an install
    # serving off :8000 had every MCP tool dialing a dead port (flywheel e2e
    # defect, 2026-08-12). Export the URL this process actually serves; an
    # already-exported DST_URL wins — that is how a proxy-fronted deployment
    # declares its public URL.
    if "DST_URL" not in os.environ:
        url = env_url or ""
        if not url or (urlparse(url).port or 8000) != args.port:
            url = f"http://localhost:{args.port}"
        os.environ["DST_URL"] = url
    if getattr(args, "daemon", False):
        return _serve_daemon(args)

    # Schema preflight, before the port probe: a server on a schema behind this build
    # serves answers correctly and loses every trace, in silence (services/db/
    # schema_state.py). `ahead` and `unknown` pass — older code on a newer schema is the
    # SAFE deploy order, and a database still coming up is not a broken one.
    state = schema_state()
    if state.status == "behind":
        print(serve_refusal(state), file=sys.stderr)
        return 1

    with _probe_socket(args.host) as probe:
        try:
            probe.bind((args.host, args.port))
        except OSError:
            pids = subprocess.run(
                ["lsof", "-ti", f":{args.port}"], capture_output=True, text=True
            ).stdout
            print(_port_busy_message(args.port, pids), file=sys.stderr)
            return 1
    # Serve the built SPA same-origin when present and not explicitly configured.
    # Source checkouts build to apps/web/dist; wheels ship it at services/web_dist
    # (release CI copies the Vite build there before `uv build`).
    #
    # The settings singleton carries it. Under --reload that is not enough: uvicorn
    # re-imports the app in a FRESH child process, which builds its own singleton, and
    # the environment is the only channel a parent has to it. That write is
    # unavoidable — so it is scoped to the case that needs a
    # subprocess, and never made when the app runs in this one.
    here = Path(__file__).resolve()
    candidates = (here.parents[2] / "apps" / "web" / "dist", here.parents[1] / "web_dist")
    if not settings.web_dist:
        for dist in candidates:
            if (dist / "index.html").exists():
                settings.web_dist = str(dist)
                if args.reload:
                    os.environ["DST_WEB_DIST"] = str(dist)
                break
    if not settings.web_dist:
        print(
            "dashboard: not bundled in this install — serving the API only "
            "(build apps/web with pnpm, or use a release wheel/image)"
        )
    else:
        # The positive line matters as much as the negative one: "serving from
        # <path>" vs silence is how an operator tells a UI deploy from an
        # API-only one without opening a browser.
        print(f"dashboard: serving from {settings.web_dist}")
    from services.build_info import GIT_DIRTY, GIT_SHA

    if GIT_SHA:  # name the build being served; wheels have no .git — no line
        print(f"build: {GIT_SHA[:12]}{' (dirty)' if GIT_DIRTY else ''}", flush=True)
    url = f"http://{args.host}:{args.port}"
    print(f"waiting for server at {url} ... (ready when /health answers)", flush=True)
    _announce_ready(url)
    try:
        uvicorn.run("services.app:app", host=args.host, port=args.port, reload=args.reload)
    except SystemExit as exc:
        # uvicorn exits 3 when the lifespan failed. A non-daemon thread the
        # failed startup left behind would keep the interpreter alive past
        # that, so the exit is forced once the log is flushed.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exc.code if isinstance(exc.code, int) else 1)
    return 0


def _db_ready() -> bool:
    from sqlalchemy import create_engine, text

    from services.config import settings

    try:
        eng = create_engine(settings.database_admin_url, connect_args={"connect_timeout": 2})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _dev(args: argparse.Namespace) -> int:
    """One command: Postgres up → migrate → serve (SPA included when built).

    Connectivity-driven: a reachable database is used as-is; otherwise the
    project's docker-compose.yml (scaffolded by `dst init`) — or the source
    checkout's — is brought up and waited on by connecting, not by poking a
    hardcoded container name."""
    import shutil
    import subprocess
    import time

    from services.config import settings

    if not _db_ready():
        repo_compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        compose_dir = next(
            (d for d in (Path.cwd(), repo_compose.parent) if (d / "docker-compose.yml").exists()),
            None,
        )
        if compose_dir is None:
            print(
                f"error: no database at {settings.database_admin_url} and no "
                "docker-compose.yml here.\nRun `dst init` (it scaffolds one) "
                "or start Postgres yourself and point DATABASE_URL/.env at it.",
                file=sys.stderr,
            )
            return 1
        # The two ways this fails on a fresh machine are not code paths, they
        # are missing prerequisites — and quickstart step three is the wrong
        # place for a traceback.
        if shutil.which("docker") is None:
            print(
                "error: docker is not installed (or not on PATH). `dst dev` starts "
                "Postgres via docker compose; install Docker Desktop or start "
                "Postgres yourself and point DATABASE_URL/.env at it.",
                file=sys.stderr,
            )
            return 1
        up = subprocess.run(["docker", "compose", "up", "-d"], cwd=compose_dir, check=False)
        if up.returncode != 0:
            print(
                "error: `docker compose up` failed (is the Docker daemon running?). "
                "Start Docker Desktop and re-run `dst dev`, or start Postgres "
                "yourself and point DATABASE_URL/.env at it.",
                file=sys.stderr,
            )
            return 1
        for _ in range(30):
            if _db_ready():
                break
            time.sleep(2)
        else:
            print("error: database did not become ready after 60s", file=sys.stderr)
            return 1
    mismatch = _sentinel_mismatch()
    if mismatch:
        print(mismatch, file=sys.stderr)
        return 1
    _migrate(args)
    return _serve(args)


def _compose_project(compose_dir: Path | None = None) -> str | None:
    """The ``name:`` of the project's docker-compose.yml, when there is one."""
    path = (compose_dir or Path.cwd()) / "docker-compose.yml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                return line.partition(":")[2].strip().strip("'\"") or None
    except OSError:
        return None
    return None


def _sentinel_mismatch() -> str | None:
    """The refusal for a database whose encryption sentinel DST_SECRET_KEY does
    not decrypt, naming the compose project the volume belongs to — else None
    (a matching key, a fresh database, or no sentinel table yet). The server
    would refuse the same way in its lifespan, but from inside uvicorn that
    refusal left a hung child behind `--daemon`; here it is a line and exit 1
    before anything starts."""
    from services.security.sentinel import WrongEncryptionKey, verify_or_install

    try:
        verify_or_install()
    except WrongEncryptionKey as exc:
        project = _compose_project()
        where = f"compose project '{project}' (its pgdata volume)" if project else "this database"
        return (
            f"error: {exc}\nThe data in {where} was written under another DST_SECRET_KEY — "
            "most likely a different project's volume. Point DATABASE_URL at this project's "
            "own database (a unique compose `name:` gives it its own volume), restore the "
            "key that wrote it, or `docker compose down -v` to start over."
        )
    except Exception:  # noqa: BLE001 — no table yet, or no crypto configured: nothing to check
        return None
    return None


def register_migrate(sub: Any) -> None:
    sub.add_parser("migrate", help="run database migrations to head").set_defaults(fn=_migrate)


def register_dev(sub: Any) -> None:
    p = sub.add_parser("dev", help="Postgres up + migrate + serve (auto-reload), one command")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default: the port in DST_URL (.env), else 8000")
    p.add_argument(
        "--daemon",
        action="store_true",
        help="detach: pid in .dst/serve.pid, output in .dst/serve.log; `dst serve --stop` ends it",
    )
    p.set_defaults(fn=_dev, reload=True)


def register_serve(sub: Any) -> None:
    p = sub.add_parser("serve", help="run the API (+ SPA when built) via uvicorn")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default: the port in DST_URL (.env), else 8000")
    p.add_argument("--reload", action="store_true")
    p.add_argument(
        "--daemon",
        action="store_true",
        help="detach: pid in .dst/serve.pid, output in .dst/serve.log; `dst serve --stop` ends it",
    )
    p.add_argument(
        "--stop", action="store_true", help="stop the background server recorded in .dst/serve.pid"
    )
    p.set_defaults(fn=_serve)


def register_mcp(sub: Any) -> None:
    p = sub.add_parser(
        "mcp",
        help="run the MCP server over stdio for a local client (Claude Desktop, Cursor): "
        "DST_API_KEY carries the caller key, DST_URL the dst base URL (env or ./.env). "
        "Remote clients use the API's own /mcp endpoint instead",
    )
    p.set_defaults(fn=_mcp)
