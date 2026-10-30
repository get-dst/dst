"""Helpers more than one `dst` command uses: URL and credential resolution,
project reading, connection lookup, and the serve/migrate plumbing."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # httpx is imported per-verb (CLI startup cost) — types only here
    import httpx

# The agent the CLI's serving requests name (`X-Dst-Agent`): the request ledger
# and `dst observe` tell a terminal ask from a caller's by it.
CLI_AGENT = "dst-cli"


def _declared_connection(
    args: argparse.Namespace, connection: str | None = None
) -> tuple[str, dict[str, object], str | None]:
    """(type, config, secret) for --connection from the project's dst.yaml, or
    ("", {}, None) when the file doesn't declare it.

    Introspect is step 1 of authoring — you introspect to LEARN what to author —
    so it must not require a prior apply (blocker 2: "unknown connection" from
    the one command the skill tells you to run first). The declaration is the
    source of truth anyway; the server is a mirror of it.

    *connection* overrides ``args.connection`` for the verbs that resolve a name
    they were not given on the command line — `plan` reads it off the recorded
    baselines under the project."""
    from services.config import resolve_env_ref
    from services.project.schema import parse_project_yaml

    path = Path(getattr(args, "dir", ".") or ".") / "dst.yaml"
    if not path.exists():
        return "", {}, None
    try:
        project = parse_project_yaml(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"warning: {path}: {exc}", file=sys.stderr)
        return "", {}, None
    decl = project.connections.get(connection or args.connection)
    if decl is None:
        return "", {}, None
    return decl.type, decl.config, resolve_env_ref(decl.secret_env, dirs=_env_dirs(args))


def _undeclared_connection(args: argparse.Namespace) -> str:
    """The one message for "nothing here knows this connection" — it names the
    file to edit, not a command to run first."""
    path = Path(getattr(args, "dir", ".") or ".") / "dst.yaml"
    return (
        f"unknown connection '{args.connection}': no `connections.{args.connection}` in "
        f"{path}, and no server has it either. Declare it there (type + config + "
        "secret_env) — introspect reads that declaration directly, before any apply."
    )


def _table(columns: list[str], rows: list[list[object]]) -> str:
    """Rows as an aligned text table — what you read to decide what a column means."""
    cells = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [max([len(c), *(len(r[i]) for r in cells)]) for i, c in enumerate(columns)]
    header = "  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip()
    body = ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join([header, *body])


def _drift_tolerance(root: Path) -> Any:
    """`probe.drift_tolerance` from dst.yaml, or the defaults when the file is
    absent or does not parse (plan reports that; drift must still run)."""
    from services.project.schema import DriftTolerance, parse_project_yaml

    path = root / "dst.yaml"
    if path.exists():
        try:
            return parse_project_yaml(path.read_text(encoding="utf-8")).probe.drift_tolerance
        except ValueError:
            pass
    return DriftTolerance()


def _bytes(n: int) -> str:
    for div, unit in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB"), (1e3, "kB")):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def _env_dirs(args: argparse.Namespace) -> tuple[str]:
    """The .env search path for a --dir verb: the named project, and ONLY it.
    One definition — every verb that takes --dir resolves secrets the same way,
    whether it talks HTTP or runs in-process.

    It used to fall through to the shell's cwd when a key was missing from
    `<dir>/.env`, which is a cross-project credential leak: `dst apply --dir
    A` run from inside project B authenticated as B — the exact failure --dir
    exists to prevent, re-entering through the fallback. A project's secrets are
    its own; when `<dir>/.env` doesn't define a key, the process env or an
    explicit flag supplies it, never the neighbouring project. Without --dir the
    path is `.` — the cwd's own project, which is not a leak."""
    return (getattr(args, "dir", None) or ".",)


def _adopt_project_env(args: argparse.Namespace) -> None:
    """The in-process half of --dir, for the verbs that talk to the database
    instead of a server. `settings` is a pydantic singleton loading `.env`
    relative to the SHELL's cwd, so `dst test --dir X` from outside X would
    sweep whatever database the shell's project uses — the wrong org's corpus,
    reported as X's. Same precedence as _client: process env wins, then
    <dir>/.env — and nothing else (see _env_dirs: the cwd's .env belongs to the
    cwd's project).

    The mechanism is services.config.adopt_project_env, which rebuilds the
    settings singleton and redirects env-ref resolution WITHOUT touching
    os.environ — see its docstring for why the old `os.environ.setdefault` had
    to go. The undo rides on `args`, so it is scoped to this invocation and
    main() unwinds it whatever the verb does (the CLI is one-shot; anything
    importing this module as a library is not)."""
    from services.config import adopt_project_env

    args.restore_project_env = adopt_project_env(_env_dirs(args))


def _envs_file(args: argparse.Namespace | None = None) -> Path:
    """The env-name → admin-token map `dst env new` records: .dst/envs.json in
    the --dir project (or cwd). Per-machine credential state, never project
    truth — `dst init` gitignores .dst/."""
    base = Path(_env_dirs(args)[0]) if args is not None else Path.cwd()
    return base / ".dst" / "envs.json"


def _envs_read(args: argparse.Namespace | None = None) -> dict[str, str]:
    path = _envs_file(args)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _resolve_env_flag(args: argparse.Namespace) -> int | None:
    """--env <name>: the local no-secret selector.

    Resolves the name through .dst/envs.json and injects the stored token as
    if --token had been typed, BEFORE any other resolution — so every
    precedence rule downstream is unchanged and no human handles the secret.
    Returns an exit code on error, None when resolved (or no flag given)."""
    name = getattr(args, "env", None)
    if not name:
        return None
    if getattr(args, "token", None):
        print(
            "error: --env and --token are two answers to the same question — pick one",
            file=sys.stderr,
        )
        return 1
    known = _envs_read(args)
    token = known.get(name)
    if token is None:
        hint = f"known here: {', '.join(sorted(known))}" if known else "none recorded here"
        print(
            f"error: no environment named {name!r} in {_envs_file(args)} ({hint}) — "
            f"`dst env new {name}` records it",
            file=sys.stderr,
        )
        return 1
    args.token = token
    return None


def _resolve_url(args: argparse.Namespace) -> tuple[str, str]:
    """The server URL and WHERE it came from. Precedence is unchanged — --url,
    then DST_URL in the process env, then DST_URL in the --dir project's
    .env (never the shell cwd's; see _env_dirs), then the built-in default.

    The provenance is the new half, and it exists because the last step is
    silent. The first three are the user's own visible configuration; the fourth
    is dst guessing. An agent that read DST_URL from its project's .env
    and then ran one command a directory out got the guess instead — same
    command, different server, nothing said — and the traceback that followed
    named neither the URL nor where it came from. So the rule is: the guess is
    never silent (it announces the URL it picked and what it looked at), and any
    failure to reach the server names the URL AND its source. A user can always
    tell which server a command targeted, and why."""
    import os

    from services.config import resolve_env_ref

    if getattr(args, "url", None):
        return str(args.url), "--url"
    dirs = _env_dirs(args)
    envfile = (Path(dirs[0]) / ".env").resolve()
    url = resolve_env_ref("DST_URL", dirs=dirs)
    if url:
        return url, (
            "DST_URL in the environment" if os.environ.get("DST_URL") else f"DST_URL in {envfile}"
        )
    default = "http://localhost:8000"
    source = (
        "the built-in default: no --url, no DST_URL in the environment, and "
        f"{envfile} {'defines none' if envfile.exists() else 'does not exist'}"
    )
    print(f"note: targeting {default} — {source}", file=sys.stderr)
    return default, source


def _client(
    args: argparse.Namespace, *, caller_key_ok: bool = False, admin_first: bool = False
) -> tuple[str, dict[str, str]]:
    """URL + auth for the HTTP-wrapper verbs. Explicit flags win; otherwise
    DST_URL and DST_ADMIN_TOKEN resolve from the process env, then the
    --dir project's .env (the gitignored secrets file — init and bootstrap write
    them), so in-project agents and humans never juggle flags per command. The
    --dir hop matters: `apply --dir X` run from outside X (CI, cron, another
    repo) must authenticate as X, not as whatever project the shell happens to
    sit in — and must NOT borrow the shell's token when X defines none.

    ``caller_key_ok`` opens the second door: a `dst_` CALLER key via --key (or
    DST_API_KEY, the same name the MCP client config uses). Admin tokens
    bypass every lens allow-list, so with only the admin door there is no
    product-native way to prove a grant works — and an agent that needs to ask AS a
    caller falls back to curling /v1 with a dst_ key by hand, because the CLI
    cannot. Explicit --key wins over the admin token, so
    a project's .env holding both still verifies as the caller when asked to.

    ``admin_first`` picks WHICH door a bare (flagless) invocation takes when the
    .env holds both, and the two verb families genuinely want opposite answers:

      * `query` (admin_first=False): the caller door is the whole point — asking
        as the admin proves nothing about a grant, so a DST_API_KEY in scope
        is preferred over the admin token.
      * `correct` / `reviews` (admin_first=True): the admin door is strictly the
        more capable one — it files against anyone's request and lists the whole
        team queue. An analyst whose .env holds an admin token AND her own caller
        key must keep triaging org-wide; silently demoting her to her own tickets
        would break the cross-party correction loop. So the caller key is the
        FALLBACK — used when no admin token is in scope, which is exactly the
        business user's posture — while an explicit --key still wins outright.

    Records the door taken on ``args.as_caller`` (like ``args.url_source``), so a
    verb whose two doors are different endpoints can route on it."""
    from services.config import resolve_env_ref

    code = _resolve_env_flag(args)
    if code is not None:
        raise SystemExit(code)
    dirs = _env_dirs(args)
    url, args.url_source = _resolve_url(args)
    args.url = url
    args.as_caller = False

    def _caller(key: str) -> tuple[str, dict[str, str]]:
        args.as_caller = True
        return args.url.rstrip("/"), {"Authorization": f"Bearer {key}"}

    explicit_key = getattr(args, "key", None) if caller_key_ok else None
    if explicit_key:
        return _caller(explicit_key)
    env_key = resolve_env_ref("DST_API_KEY", dirs=dirs) if caller_key_ok else None
    # An explicit --env targets THAT org's admin door; letting an ambient
    # caller key win here would silently query the default org instead.
    if env_key and not admin_first and not getattr(args, "env", None):
        return _caller(env_key)
    token = getattr(args, "token", None) or resolve_env_ref("DST_ADMIN_TOKEN", dirs=dirs)
    if token:
        return args.url.rstrip("/"), {"Authorization": f"Bearer {token}"}
    if env_key:
        return _caller(env_key)
    extra = " or a caller key with --key (dst_…)" if caller_key_ok else ""
    print(
        f"error: no admin token{extra} — pass --token, or run `dst bootstrap` in "
        "this project (it saves DST_ADMIN_TOKEN to .env for you)",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _detail(response: httpx.Response) -> str:
    """The server's ``detail``, else a clipped body — one copy of the shape the
    HTTP verbs here hand-roll, so a new verb's failure never becomes a
    traceback."""
    try:
        return str(response.json().get("detail") or response.text[:200])
    except Exception:  # noqa: BLE001 — non-JSON error bodies happen on 5xx
        return response.text[:200]


def _unreachable_exit(exc: BaseException, args: argparse.Namespace) -> int | None:
    """One named line for a request that never got an answer — or None when this
    is not that kind of failure, and the caller re-raises it.

    Every server-bound verb is an httpx call to the URL `_client` resolved, and
    none of them caught a TRANSPORT failure: with no server up, `dst plan`,
    `keys`, `query` — all of them — printed a 69-line httpx.ConnectError
    traceback. plan and apply catch `httpx.TimeoutException` and always did
    (the docs read that as covering this; it never did — a slow server and an
    absent one are different exceptions). Handled once here at the dispatch,
    because "remember to catch it" is not a rule 16 verbs can keep.

    The URL comes off the failed REQUEST, so an unrelated httpx call — a
    provider endpoint an in-process verb reaches — is never mislabelled as the
    dst server; only a failure to the URL this command resolved gets the
    "is one running?" advice."""
    if "httpx" not in sys.modules:  # imported per-verb: unimported = no request was made
        return None
    import httpx

    if not isinstance(exc, httpx.RequestError):
        return None
    try:
        target = str(exc.request.url)
    except RuntimeError:  # httpx raises when the error carries no request
        target = ""
    server = (getattr(args, "url", None) or "").rstrip("/")
    cause = str(exc) or type(exc).__name__
    if not server or not target.startswith(server):
        print(f"error: request to {target or 'the server'} failed — {cause}", file=sys.stderr)
        return 1
    if isinstance(exc, httpx.TimeoutException) and not isinstance(exc, httpx.ConnectTimeout):
        print(
            f"error: {server} accepted the connection but did not answer in time — {cause}. "
            "The server may still be working on it; raise --timeout to wait longer.",
            file=sys.stderr,
        )
        return 1
    # WHERE the URL came from, always: "localhost:8000" alone never told the
    # reader that nothing they wrote had chosen it (_resolve_url).
    print(
        f"error: could not reach a dst server at {server} — {cause}. Nothing was sent. "
        f"That URL came from {getattr(args, 'url_source', 'this command')}. "
        "Start one with `dst dev` (or `dst serve`), or point this command at "
        "yours: DST_URL in the project's .env, or --url.",
        file=sys.stderr,
    )
    return 1


def _read_project(root: Path) -> dict[str, str]:
    # profiles/ rides along for apply's probe-artifact ingestion; the drift
    # baseline in the same dir is harmless there (only *.probe.json is read).
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and p.relative_to(root).as_posix().startswith(("lenses/", "semantic/", "profiles/"))
    }
    if (root / "dst.yaml").exists():
        files["dst.yaml"] = (root / "dst.yaml").read_text(encoding="utf-8")
    # A Cube-backed project's overlay rides along; the model under cube/model/
    # never does — dst reads it from Cube's /v1/meta, as Cube compiled it.
    if (root / "cube" / "overlay.yaml").exists():
        files["cube/overlay.yaml"] = (root / "cube" / "overlay.yaml").read_text(encoding="utf-8")
    return files


def _declared_warehouses(root: Path) -> list[str] | None:
    """The warehouse connections dst.yaml declares, or None when it is absent or
    malformed — plan already reports a broken dst.yaml; this check must not."""
    from services.project.compile import CompileError, dialect_for
    from services.project.schema import parse_project_yaml

    path = root / "dst.yaml"
    if not path.exists():
        return None
    try:
        project = parse_project_yaml(path.read_text(encoding="utf-8"))
    except ValueError:
        return None

    def _is_warehouse(conn_type: str) -> bool:
        try:
            dialect_for(conn_type)
        except CompileError:
            return False  # a context source — drift has nothing to watch
        return True

    return [n for n, d in project.connections.items() if _is_warehouse(d.type)]


def _project_org(args: argparse.Namespace) -> tuple[uuid.UUID, str] | None:
    """THE org a project-local verb acts on — never "every tenant in the database".

    One definition, because two verbs already got this wrong independently:
    `dst test` swept every org and filtered on lens NAME alone (fixed in
    6c7c8c4), and `dst revoke-key` ran its UPDATE with no org predicate at
    all, so revoking one tenant's caller revoked every same-named caller in the
    install. A verb that resolves its own scope is a verb that can get it wrong.

    Precedence mirrors _client's: an explicit ``--org NAME``, else the org this
    project's DST_ADMIN_TOKEN authenticates as — the same credential apply
    and reviews read from the same .env, so a project-local verb acts on exactly
    what the project pushes. Duplicate org names resolve oldest-first, the rule
    `_bootstrap` already uses.

    None when neither resolves, WITHOUT printing: the caller decides what that
    means. A read-only sweep may widen loudly (`dst test`); an
    access-control verb must refuse (`dst revoke-key`)."""
    from sqlalchemy import text

    from services.auth.tokens import hash_token
    from services.config import resolve_env_ref
    from services.db.session import admin_engine

    # --env on a DB-direct verb IS the org selector: `dst env new x` creates
    # an org named x, so the name scopes without any token changing hands —
    # the local no-secret path.
    if not args.org and getattr(args, "env", None):
        args.org = args.env

    with admin_engine.connect() as c:
        if args.org:
            row = c.execute(
                text("SELECT id, name FROM org WHERE name = :n ORDER BY created_at LIMIT 1"),
                {"n": args.org},
            ).first()
        elif token := resolve_env_ref("DST_ADMIN_TOKEN", dirs=_env_dirs(args)):
            row = c.execute(
                text(
                    "SELECT o.id, o.name FROM org o JOIN admin_token t ON t.org_id = o.id "
                    "WHERE t.token_hash = :h AND t.revoked_at IS NULL"
                ),
                {"h": hash_token(token)},
            ).first()
        else:
            return None
    return (row[0], row[1]) if row is not None else None
