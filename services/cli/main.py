"""The `dst` CLI — thin orchestration over code that already exists.

Server-side commands (migrate, bootstrap, demo, serve) run in-process against
the configured database. Project commands (export, plan, apply) are HTTP
wrappers over /mgmt/project/* — auth and RLS stay server-side.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.cli import style

if TYPE_CHECKING:  # httpx is imported per-verb (CLI startup cost) — types only here
    import socket
    from collections.abc import Callable
    from typing import TextIO

    import httpx

    from services.db.schema_state import SchemaState

# How long `plan` will wait on the warehouse before it drops the staleness check
# and says nothing. plan is the verb an engineer runs every day and the check is a
# courtesy, so a warehouse that accepts the connection and then never answers must
# cost this much and not one second more.
PLAN_WAREHOUSE_TIMEOUT = 5.0

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


def _doctor(args: argparse.Namespace) -> int:
    """Callability, not configuration: /ready reports what is
    CONFIGURED and is deliberately never a readiness gate, so a fully-configured
    but uncallable provider (SDK incompatibility, bad key, wrong base_url) was
    only discoverable via a real query's 500 and a server-log traceback. One
    cheap REAL call per model tier — max_tokens=1 — catches the whole class for
    a fraction of a cent. In-process like `dst test`; connections are already
    probed by every apply and are not re-probed here."""
    _adopt_project_env(args)
    from services.contracts.protocols import Message
    from services.db.schema_state import schema_state
    from services.llm import registry

    failures = 0
    try:
        state = schema_state()
        print(f"db            {state.summary()}")
        if state.status == "behind":
            failures += 1
    except Exception as exc:  # noqa: BLE001 — every probe reports, none aborts the report
        print(f"db            FAIL — {exc}")
        failures += 1
    try:
        embedder = registry.resolve_embedder()
        print(
            "embeddings    ok"
            if embedder is not None
            else "embeddings    not configured — certified matching off"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"embeddings    FAIL — {exc}")
        failures += 1
    print("providers")
    for tier_name in ("fast", "smart"):
        ref = registry.tier(tier_name)
        pair = registry.resolve(ref)
        if pair is None:
            detail = registry.unservable_detail(ref) or "no provider configured"
            print(f"  {tier_name:<6}{ref or '(unset)'}  SKIP — {detail}")
            continue
        try:
            pair.llm.complete(
                system=[],
                messages=[Message(role="user", content="ping")],
                model=pair.name,
                temperature=0.0,
                # 8, not 1: a 1-token cap is consumed before any content, so
                # completion_text logs an 'empty completion' warning on a call
                # that SUCCEEDED — a false alarm in the one verb whose job is to
                # say what works. Still a fraction of a cent; this probes
                # callability, not output.
                max_tokens=8,
            )
            print(f"  {tier_name:<6}{pair.name}  ok")
        except Exception as exc:  # noqa: BLE001 — the whole point is naming this
            failures += 1
            print(f"  {tier_name:<6}{pair.name}  FAIL — {exc}")
    # The scaffold's own currency. Never a failure — an author may be holding an
    # older skill deliberately, and this verb's exit code means "callable", not
    # "up to date". It exists because an init-time snapshot outliving its
    # release had no reader at all (#51).
    from services.cli.init import stale_agent_files

    root = Path(getattr(args, "dir", ".") or ".")
    if (root / "dst.yaml").exists():
        stale = stale_agent_files(root)
        print(
            f"skills        {len(stale)} behind this dst — `dst init . --skills-only`"
            if stale
            else "skills        current"
        )
    if failures:
        print(style.bad(f"doctor: {failures} check(s) failed"), file=sys.stderr)
    return 1 if failures else 0


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


def _secret(args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def _rotate_key(args: argparse.Namespace) -> int:
    """`dst rotate-key` — re-encrypt every stored secret under the primary key.

    Step two of a three-step rotation, and the step that was missing entirely:

        DST_SECRET_KEY=<new>,<old>   deploy — everything still decrypts
        dst rotate-key               this verb
        DST_SECRET_KEY=<new>         drop the old key

    Refuses to run with a single key configured. Not pedantry: with only <new>
    set, nothing encrypted under <old> can be read, so the "rotation" would report
    success having silently skipped every row it could not decrypt — the exact
    shape of Metabase's rotation bug, where a partial re-encrypt looked clean.
    """
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.db.session import admin_engine
    from services.security import crypto
    from services.security.sentinel import rotate_sentinel

    crypto.reset_cache()
    if not crypto.is_configured():
        print("error: DST_SECRET_KEY is not set", file=sys.stderr)
        return 1
    if crypto.key_count() < 2 and not args.force:
        print(
            "error: only one key in DST_SECRET_KEY. Rotation needs both:\n"
            "  DST_SECRET_KEY=<new>,<old>\n"
            "Rotating with one key would skip every row it cannot decrypt and still "
            "report success. Pass --force only to re-encrypt under the same key.",
            file=sys.stderr,
        )
        return 1

    rotated = failed = 0
    with admin_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, secret_encrypted FROM connection WHERE secret_encrypted IS NOT NULL")
        ).all()
        for row_id, blob in rows:
            try:
                conn.execute(
                    text("UPDATE connection SET secret_encrypted = :v WHERE id = :i"),
                    {"v": crypto.rotate(str(blob)), "i": row_id},
                )
                rotated += 1
            except crypto.CryptoNotConfigured:
                # Named, never swallowed: a row we cannot decrypt is a row whose
                # key is not in the list, and the operator has to know before they
                # drop the old key.
                print(f"  connection {row_id}: NOT decryptable with any configured key")
                failed += 1
    rotate_sentinel()

    print(f"re-encrypted {rotated} stored secret(s) under the primary key")
    if failed:
        print(
            f"{failed} row(s) could not be decrypted — do NOT drop the old key yet",
            file=sys.stderr,
        )
        return 1
    print("safe to drop the old key from DST_SECRET_KEY")
    return 0


def _bootstrap(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from services.auth import tokens
    from services.db.session import admin_engine

    password = args.password
    if args.email and not password:
        if sys.stdin.isatty():
            import getpass

            password = getpass.getpass(f"password for {args.email}: ")
        else:
            print("error: --email needs --password (or an interactive terminal)", file=sys.stderr)
            return 1

    # Idempotent: rerunning bootstrap reuses the org (oldest wins if legacy duplicates
    # exist) and the user, and only ever mints a fresh admin token — running it twice
    # must never leave two orgs both named "default" with split credentials.
    raw = tokens.new_admin_token()
    with admin_engine.begin() as c:
        org_id = c.execute(
            text("SELECT id FROM org WHERE name = :n ORDER BY created_at LIMIT 1"),
            {"n": args.org},
        ).scalar()
        org_created = org_id is None
        if org_id is None:
            org_id = c.execute(
                text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": args.org}
            ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, :l)"),
            {"o": org_id, "h": hashlib.sha256(raw.encode()).hexdigest(), "l": "bootstrap"},
        )
    if args.email:
        from services.auth import local
        from services.db.session import org_session

        with org_session(org_id) as session:
            uid = local.user_id_by_email(session, args.email)
            if uid is None:
                local.create_user(session, args.email, password, role="admin")
            else:
                session.execute(
                    text("UPDATE local_user SET password_hash = :h, role = 'admin' WHERE id = :i"),
                    {"h": local.hash_password(password), "i": uid},
                )
            session.commit()
    print(
        f"org: {args.org} ({org_id}) — "
        + (
            "created"
            if org_created
            # Probe finding: say what re-running actually did to credentials —
            # a NEW token was minted into .env; previously issued ones stay valid.
            else "reused (new admin token saved to .env; earlier tokens stay valid)"
        )
    )
    print(f"admin token (store it now — shown once): {raw}")
    envfile = Path(".env")
    if envfile.exists():
        # The project's gitignored secrets file — saving the token here is what
        # lets every later command (and the in-project agent) skip --token.
        kept = [
            line
            for line in envfile.read_text(encoding="utf-8").splitlines()
            if not line.startswith("DST_ADMIN_TOKEN=")
        ]
        kept.append(f"DST_ADMIN_TOKEN={raw}")
        envfile.write_text("\n".join(kept) + "\n", encoding="utf-8")
        print("saved to .env as DST_ADMIN_TOKEN — dst commands here read it automatically")
    if args.email:
        print(f"dashboard login: {args.email} (admin)")
    return 0


def _demo(args: argparse.Namespace) -> int:
    from services.config import settings
    from services.db.session import org_session
    from services.lenses import connection_store, store
    from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
    from services.semantic import store as semantic_store

    with org_session(args.org_id) as session:
        if not connection_store.get_connection(session, "jaffle"):
            connection_store.create_connection(
                session, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
            )
        # The demo dogfoods the shared layer: seed its assets so the published
        # bundle's compile provenance matches the store (never spuriously stale).
        entities, definitions = jaffle_shared_assets()
        for e in entities:
            semantic_store.upsert_asset(session, "entity", e.name, e.model_dump(mode="json"))
        for d in definitions:
            semantic_store.upsert_asset(session, "definition", d.term, d.model_dump(mode="json"))
        bundle = jaffle_customer_value_bundle()
        if not store.lens_exists(session, bundle.config.name):
            store.create_lens(session, bundle)
        store.publish(session, bundle.config.name)
        session.commit()
    print(f"demo lens '{jaffle_customer_value_bundle().config.name}' published (duckdb jaffle)")
    return 0


def _reindex(args: argparse.Namespace) -> int:
    from services.db.reindex import run_reindex
    from services.llm import registry

    embedder = registry.resolve_embedder()
    if embedder is None:
        print(f"error: {registry.EMBEDDER_HINT}", file=sys.stderr)
        return 1
    return run_reindex(embedder, batch=args.batch)


# A query that produced no SQL exits NON-ZERO. Exit 0 used to mean "the request
# reached the server", so a parser error, a guard rejection and a provider timeout
# all came back as exit 0 with the reason buried in English in `answer` — the
# silent-empty shape, and unbranchable for the agents this command exists for. The
# scaffold already teaches "check the exit code, not just the diffs"
# (services/cli/init.py:509); now the exit code is worth checking.
#
# Three classes, because collapsing them would cost the distinction the product is
# built on. A DECLINE is not a failure: refusing a question the lens cannot answer,
# and asking which meaning of an ambiguous term you want, are the governed
# behaviors dst exists to provide — an agent that sees 3 should rephrase or ask
# its user, while one that sees 1 should retry or escalate. 2 is argparse's own
# usage-error code and is deliberately skipped.
EXIT_DECLINED = 3
_QUERY_EXIT: dict[str, int] = {
    "ok": 0,
    "refused": EXIT_DECLINED,
    "clarification": EXIT_DECLINED,
    "rejected": 1,
    "error": 1,
}


def _freshness_part(d: dict[str, object]) -> str | None:
    """The measured freshness floor, and loudly when it breaks the lens's declared
    contract. `data_as_of` rode the API response from day one and the terminal
    never printed it — the one field a human can't sanity-check from the answer."""
    as_of = d.get("data_as_of")
    if not as_of:
        return None
    verification = d.get("verification")
    raw_checks = verification.get("checks") if isinstance(verification, dict) else None
    checks = raw_checks if isinstance(raw_checks, list) else []
    stale = any(
        isinstance(c, dict) and c.get("name") == "freshness" and c.get("status") == "fail"
        for c in checks
    )
    if stale:
        return style.warn(f"as of {as_of} — stale")
    return style.accent("as of:") + f" {as_of}"


def _query(args: argparse.Namespace) -> int:
    """Ask a governed question from the terminal — the verify step of the
    authoring loop as one command instead of a source-.env-and-curl incantation
    (in-project agents were literally doing that).

    ``--key dst_…`` asks AS a caller instead of as the admin, which is the only
    way to prove an access grant: an admin token bypasses every allow-list, so
    "granted B, does it work?" and "is C still denied?" both answer 200 under
    the admin door. That gap sent agents back to curl."""
    import httpx

    url, headers = _client(args, caller_key_ok=True)
    r = httpx.post(
        f"{url}/v1/lenses/{args.lens}/query",
        headers=headers,
        json={"q": args.question},
        timeout=args.timeout,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail") or r.json().get("error", {}).get("message")
        except Exception:  # noqa: BLE001 — non-JSON error bodies
            detail = r.text[:200]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    d = r.json()
    # Unknown status → treat as a failure, never as an answer: a caller reading a
    # newer server must not be told "answered" by an old client's ignorance.
    code = _QUERY_EXIT.get(str(d.get("status") or "ok"), 1)
    if args.json:
        print(json.dumps(d, indent=2))
        return code
    clarification = d.get("clarification")
    if clarification:
        print(f"clarify: {clarification['question']}")
        for option in clarification.get("options", []):
            print(f"  - {option}")
        _print_request_id(d)
        return code
    if code != 0:
        # stdout stays the answer channel and nothing else, so `$(dst query …)`
        # is either an answer or empty — never a sentence about why there isn't one.
        # The request_id rides on stderr with it: a refusal is exactly the kind of
        # response `dst correct` gets filed against.
        print(f"{d.get('status')}: {d.get('answer') or ''}", file=sys.stderr)
        _print_request_id(d, stream=sys.stderr)
        return code
    print(d.get("answer") or "")
    if d.get("sql"):
        print(f"\n{style.accent('sql:')} {style.dim(d['sql'])}")
    if d.get("trust_summary"):
        # The basis, in the author's words — not just an identifier. Two people
        # holding different "revenue" numbers reconcile on this line or not at all:
        # `definition: net_revenue` below names the winner, but only "money
        # actually collected, minus anything refunded" tells a commercial director
        # why his figure differs from his colleague's.
        print(f"{style.accent('basis:')} {d['trust_summary']}")
    meta = [
        part
        for part in (
            style.accent("confidence:") + f" {_confidence_word(str(d['confidence']))}"
            if d.get("confidence")
            else None,
            style.good(f"certified ({d['certification']})")
            if d.get("certification") not in (None, "none")
            else None,
            style.accent("definition:") + f" {d['definition_used']}"
            if d.get("definition_used")
            else None,
            _freshness_part(d),
        )
        if part
    ]
    if meta:
        print(" · ".join(meta))
    _print_request_id(d)
    return code


def _define(args: argparse.Namespace) -> int:
    """Print what a governed term MEANS — the read door as a one-liner.

    The sibling of `dst query`: query answers questions ABOUT DATA (governed
    SQL, a warehouse execution); define returns the approved definition verbatim —
    no generation, no warehouse, nothing billed. Deliberately an index lookup, not
    a search: top-k retrieval over the same governed vocabulary returns near
    neighbours and can miss the exact term that was asked for, which is the one
    thing this verb must never do. Exit 1 when nothing is governed, so agents
    branch on the code instead of parsing prose."""
    import httpx

    url, headers = _client(args, caller_key_ok=True)
    r = httpx.get(
        f"{url}/v1/definitions", headers=headers, params={"q": args.term}, timeout=args.timeout
    )
    if r.status_code >= 400:
        print(f"error: {_detail(r)}", file=sys.stderr)
        return 1
    payload = r.json()
    hits = payload.get("definitions") or []
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if hits else 1
    if not hits:
        # Mirrors the MCP tool's empty-case note: an ungoverned term is a fact to
        # report, never a meaning to invent.
        print(f"no governed definition mentions '{args.term}'", file=sys.stderr)
        return 1
    for i, d in enumerate(hits):
        if i:
            print()
        badge = "" if d.get("status") == "active" else f" [{d.get('status')}]"
        print(f"{d['term']}{badge}  (lens: {d['lens']})")
        for line in (d.get("body") or "").strip().splitlines():
            print(f"  {line}")
        if d.get("possible_mappings"):
            print(f"  possible mappings: {', '.join(d['possible_mappings'])}")
        if d.get("cites"):
            print(f"  cites: {', '.join(d['cites'])}")
    return 0


def _confidence_word(word: str) -> str:
    """The confidence grade, painted semantically — the one line that used to
    look identical at every grade."""
    if word == "verified":
        return style.good(word)
    if word == "partial":
        return style.warn(word)
    if word == "unverified":
        return style.bad(word)
    return word


def _print_request_id(response: dict[str, object], *, stream: TextIO | None = None) -> None:
    """The correction loop's only input, on the human path too. `dst correct`
    takes a request_id and nothing else, and it used to be reachable only through
    `--json` — so the loop's step 3 asked you to re-run step 2 differently.

    Follows the answer onto whichever stream carried it, so the non-zero paths do
    not put a line on stdout that `$(dst query …)` would capture."""
    if response.get("request_id"):
        print(f"request_id: {response['request_id']}", file=stream or sys.stdout)


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


def _sql(args: argparse.Namespace) -> int:
    """Read-only SQL against a connection — the governed version of opening DuckDB.

    `introspect` says what the columns ARE; it cannot say what they contain
    together, and deciding a business rule ("is a refund a negative amount, or a
    row with status='refunded'?") takes five actual rows. Authors did that
    anyway, outside the guard and outside the audit log. Same read, run through
    sql_guard, row-capped, and logged to request_log.

    Complements `introspect --profile` rather than repeating it: per-column enum
    values, null rates and ranges come from the profile in one pass; this is for
    rows, cross-column facts, and joins a profile cannot see."""
    import httpx

    # An EXPLICIT --key is always sent, even with --connection, so the refusal comes
    # from the server ("probing a whole connection needs an admin token — pass `lens`")
    # instead of a generic "no admin token" that never mentions the key you passed.
    # The DST_API_KEY fallback stays scoped to --lens: a project .env holding one
    # must not silently downgrade an admin's connection probe.
    url, headers = _client(args, caller_key_ok=bool(args.lens or args.key))
    scope = {"connection": args.connection} if args.connection else {"lens": args.lens}
    r = httpx.post(
        f"{url}/v1/sql",
        headers=headers,
        json={"sql": args.sql, "limit": args.limit, **scope},
        timeout=args.timeout,
    )
    if r.status_code == 404 and args.connection and _declared_connection(args)[0]:
        path = Path(getattr(args, "dir", ".") or ".") / "dst.yaml"
        print(
            f"error: connection '{args.connection}' is declared in {path} but no server has "
            "it yet — run `dst apply`. (`dst introspect` reads the file directly; "
            "this command runs on the server, which is what puts the probe in the audit log.)",
            file=sys.stderr,
        )
        return 1
    if r.status_code >= 400:
        detail = _detail(r)
        print(f"error: {detail}", file=sys.stderr)
        # The refusal named what is disallowed and not what to reach for, and the
        # measured next move was a raw DuckDB client — the one thing this verb
        # exists to make unnecessary. A non-SELECT here is nearly always "what
        # tables/columns are there", which is a different verb, not a workaround.
        if "only SELECT queries are allowed" in detail:
            conn = args.connection or "<name>"
            print(
                f"  to list tables and columns, run `dst introspect --connection {conn}`"
                " (add --profile for row counts, enum values and null rates)",
                file=sys.stderr,
            )
        return 1
    d = r.json()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    if d["columns"]:
        print(_table(d["columns"], d["rows"]))
    tail = f"({d['row_count']} rows"
    print(tail + "; capped — raise --limit for more)" if d["truncated"] else tail + ")")
    return 0


def _check_joins(connector: object, root: Path) -> int:
    """Measure every declared join's real cardinality against the warehouse.

    The intent compiler now DECIDES on `relationship`: a hop declared many_to_one is
    joined directly, and a lie there multiplies every additive aggregate downstream
    (a fanout can report a true 1,359,168 in deposits as 9,514,176). Nothing else
    checks the declaration, and 8 of 101 foreign keys in BIRD are not one-to-many in
    the data — so the claim needs measuring, not trusting.

    Read-only and outside apply on purpose: this is authoring feedback, and apply's
    warehouse-touching stages are not a place to add work.
    """
    import sqlglot
    from sqlglot import exp

    from services.project.loader import split_semantic
    from services.semantic.cardinality import measure, verdict
    from services.semantic.files import parse_semantic_files

    entities, _definitions = parse_semantic_files(split_semantic(_read_project(root)))
    if not entities:
        print(f"error: no semantic/entities/*.yaml under {root}", file=sys.stderr)
        return 1

    def run(sql: str) -> list[tuple[Any, ...]]:
        return list(connector.execute(sql, read_only=True).rows)  # type: ignore[attr-defined]

    worst = 0
    checked = 0
    for entity in entities.values():
        for join in entity.joins:
            other = entities.get(join.right)
            if other is None:
                print(f"[unknown  ] {entity.name} -> {join.right}: no such entity", file=sys.stderr)
                worst = max(worst, 1)
                continue
            # EVERY column each side matches on: a composite key measured one column
            # at a time reports a fan-out that the full key does not have (a prices
            # table with 2 rows per market_date, but 1 per (ticker, market_date)).
            columns: dict[str, list[str]] = {}
            for c in sqlglot.parse_one(join.on, read="duckdb").find_all(exp.Column):
                owned = columns.setdefault(c.table or "", [])
                if c.name not in owned:
                    owned.append(c.name)
            left_col = columns.get(entity.name, [])
            right_col = columns.get(join.right, [])
            if not left_col or not right_col:
                print(
                    f"[skipped  ] {entity.name} -> {join.right}: could not read one column per "
                    f"side out of `{join.on}`",
                    file=sys.stderr,
                )
                continue
            checked += 1
            status, message = verdict(
                join.relationship,
                measure(
                    run,
                    left_table=entity.source.table,
                    left_columns=left_col,
                    right_table=other.source.table,
                    right_columns=right_col,
                    left=entity.name,
                    right=join.right,
                ),
            )
            print(f"[{status:9s}] {message}")
            if status in ("wrong", "unsafe"):
                worst = 1
    if not checked:
        print("no declared joins to check — joins live on the FK-side entity", file=sys.stderr)
    return worst


def _introspect(args: argparse.Namespace) -> int:
    """Schema + profile facts for a connection, agent-legible — the raw material
    for authoring semantic/ files with your own agent.

    Resolves the connection from the project's dst.yaml first (so it works
    before the first apply), and falls back to the server — which is also where
    stored profile facts live, and the only path when the warehouse is reachable
    from the server but not from here.

    ``--profile`` runs the catalog + sampling passes against the warehouse right
    here. Without it the file-first path has no facts to print and says so: every
    scaffolded project declares its connection in dst.yaml, so this branch
    always wins and the stored-profile facts were unreachable from the CLI.

    Empty output is never success: a warehouse that yields no table says so on
    stderr, naming the schemas searched, and exits non-zero (blocker 1 — three
    agents got a blank line and exit 0 and abandoned the path)."""
    import httpx

    from services.config import resolve_env_ref
    from services.contracts.profile import TableProfile
    from services.semantic.introspect import empty_listing_reason, schema_json, serialize_schema

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else None
    type_, config, secret = _declared_connection(args)
    if not type_ and not (
        getattr(args, "token", None) or resolve_env_ref("DST_ADMIN_TOKEN", dirs=_env_dirs(args))
    ):
        # Nothing declares it and there is no server to ask: the missing
        # declaration is the actual problem, not the missing token.
        print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
        return 1
    if type_:
        from services.contracts.protocols import TargetedIntrospect
        from services.lenses.connections import build_connector, config_warnings

        for note in config_warnings(type_, config):
            print(f"warning: {note}", file=sys.stderr)
        try:
            connector = build_connector(type_, config, secret)
            # --tables resolves against the FULL catalog where the connector can
            # (a capped listing poisoned every match on a wide warehouse).
            if tables and isinstance(connector, TargetedIntrospect):
                snapshot = connector.introspect_tables(tables)
            else:
                snapshot = connector.introspect()
        except Exception as exc:  # noqa: BLE001 — a local dead end must not end the command
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            print(
                f"warning: connection '{args.connection}' declared in dst.yaml did not "
                f"introspect from here ({first}) — trying the server",
                file=sys.stderr,
            )
        else:
            reason = empty_listing_reason(args.connection, snapshot, tables)
            if reason is not None:
                print(f"error: {reason}", file=sys.stderr)
                return 1
            profiles: list[TableProfile] = []
            if args.profile:
                from services.lenses.profiler import profile_connection

                try:
                    profiles = profile_connection(connector, args.connection, tables)
                except Exception as exc:  # noqa: BLE001 — a failed sample must not eat the schema
                    first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                    print(
                        f"warning: --profile did not complete ({first}) — printing schema only",
                        file=sys.stderr,
                    )
            if args.check_joins:
                return _check_joins(connector, Path(args.dir))
            if args.as_json:
                print(json.dumps(schema_json(snapshot, profiles, tables), indent=2))
            else:
                print(serialize_schema(snapshot, profiles, tables))
            return 0

    url, headers = _client(args)
    params = {"connection": args.connection}
    if args.tables:
        params["tables"] = args.tables
    r = httpx.get(f"{url}/mgmt/semantic/introspect", headers=headers, params=params, timeout=120)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = r.text[:200]
        if r.status_code == 400:  # unknown to the server AND undeclared in the file
            detail = _undeclared_connection(args)
        print(f"error: {detail}", file=sys.stderr)
        return 1
    payload = r.json()
    if args.as_json:
        structured = payload.get("json")
        if structured is None:
            print(
                f"error: {url} does not serve --json for introspect (it predates the "
                "structured listing) — drop --json, or upgrade the server",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(structured, indent=2))
        return 0
    text = payload["text"]
    if not text.strip():
        print(f"error: connection '{args.connection}' listed no tables", file=sys.stderr)
        return 1
    print(text)
    return 0


def _drift(args: argparse.Namespace) -> int:
    """What changed in the warehouse since this project recorded it — crossed with
    the semantic assets that read the tables that changed.

    The verb exists because every other change-detection surface was off the daily
    path: `introspect` prints a snapshot and never a diff, `dst test` compares
    certified SQL against generation with both sides reading TODAY's warehouse (so
    a consistently wrong definition passes 1/1), and profile drift was REST-only.
    So a layer keeps serving its day-1 derivation of "discount" long after the
    warehouse publishes the company's own `discount_amount` column — confidently
    off by whatever the derivation missed, on every answer, until somebody notices.

    The cross-reference is the feature. A bare column list is what `introspect`
    already printed and nobody read; "`ops.orders` gained `discount_amount`, and
    definition `discount` derives that quantity from `list_price - unit_price`" is
    a line somebody acts on.

    File-first: the baseline is `profiles/<connection>.json` in the project, so
    this runs before any apply, without a server, and lands in git next to the
    layer it describes. `dst probe` arms it (one artifact family); `--accept` is
    the ONLY writer here — a bare run never records, because self-baselining an
    already-mutated warehouse destroys the evidence it exists to keep (a
    self-baselining first run silently accepts the very drift it was asked about).

    Exit codes (a gate reads them): 0 clean · 2 changes, none breaking ·
    1 changes breaking declared references (entities/definitions/certified) —
    operational errors also exit 1, the act-now side · 4 not armed.
    """
    from services.lenses.connections import build_connector
    from services.lenses.profiler_catalog import catalog_profiles
    from services.project import warehouse_drift as wd
    from services.project.loader import split_semantic

    root = Path(args.dir or ".")
    type_, config, secret = _declared_connection(args)
    if not type_:
        # No server fallback: the stored profile the server would diff against is
        # one generation deep and rewritten by any pass that upserts, so it cannot
        # answer "since I authored this". The declaration is what drift needs.
        print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
        return 1
    try:
        connector = build_connector(type_, config, secret)
        current = catalog_profiles(connector, args.connection)
    except Exception as exc:  # noqa: BLE001 — a warehouse dead end must name itself
        first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        print(
            f"error: connection '{args.connection}' did not profile from here ({first}) — "
            "drift compares the live catalog against the recorded one and cannot "
            "report on a warehouse it could not read",
            file=sys.stderr,
        )
        return 1

    project_files = _read_project(root)
    entities, definitions = wd.parse_layer(split_semantic(project_files))
    baseline = wd.read_baseline(root, args.connection)
    if baseline is None:
        if not args.accept:
            # Unarmed is its own exit code, and a bare run NEVER writes: the
            # first run against an already-mutated warehouse would record the
            # mutation as normal and report clean forever after.
            print(
                f"error: drift is not armed for '{args.connection}' — no baseline recorded. "
                "Run `dst probe` (or `dst drift --accept`) to record the warehouse as it "
                "stands now; the next run reports what changed against it.",
                file=sys.stderr,
            )
            return 4
        path = wd.write_baseline(root, args.connection, current)
        if args.as_json:
            print(json.dumps({"connection": args.connection, "recorded": len(current)}, indent=2))
        else:
            print(
                f"note: recorded {len(current)} table(s) for '{args.connection}' in {path}. "
                "Commit it: the next run reports what changed against it, and "
                "`dst plan` says so on its own.",
                file=sys.stderr,
            )
        return 0

    findings = wd.cross_reference(
        wd.baseline_drift(baseline.tables, current),
        entities,
        definitions,
        wd.parse_certified(project_files),
    )
    if args.as_json:
        print(
            json.dumps(
                {
                    "connection": args.connection,
                    "baseline_recorded_at": baseline.recorded_at.isoformat(),
                    "tables": len(current),
                    "findings": [
                        {**f.model_dump(), "breaking": wd.is_breaking(f)} for f in findings
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    elif not findings:
        # Silence on stdout is the point: this runs in a loop, and a verb that
        # prints a wall on a quiet day is one people stop reading on a loud one.
        print(
            f"note: no schema change in '{args.connection}' since "
            f"{baseline.recorded_at.date().isoformat()} ({len(current)} tables)",
            file=sys.stderr,
        )
    else:
        for finding in findings:
            for line in wd.render(finding):
                print(line)
        read = sum(1 for f in findings if f.referenced)
        broken = sum(1 for f in findings if wd.is_breaking(f))
        verdict = f", {broken} BREAKING declared references" if broken else ""
        print(
            f"\n{len(findings)} schema change(s) since "
            f"{baseline.recorded_at.date().isoformat()}, {read} on tables the semantic "
            f"layer reads{verdict}. Re-record with `dst drift --accept` once reviewed."
        )
    if args.accept:
        if findings:
            # Nothing changed means nothing to accept: re-recording anyway would put a
            # timestamp-only commit in the history of a file whose history IS the point.
            wd.write_baseline(root, args.connection, current)
        return 0
    return wd.exit_code(findings)


def _sample_scope(
    args: argparse.Namespace, profiles: list[Any], entities: list[Any], connection: str
) -> set[str]:
    """Which tables the sampling pass reads: an explicit --tables wins, then the
    tables the semantic layer maps on this connection, then — no layer to scope
    by — everything (the bootstrap probe that precedes authoring)."""
    from services.project.warehouse_drift import same_table

    if getattr(args, "tables", None):
        wanted = {t.strip() for t in args.tables.split(",") if t.strip()}
        return {p.table for p in profiles if p.table in wanted}
    if getattr(args, "sample_all", False):
        return {p.table for p in profiles}
    mapped = {
        p.table
        for p in profiles
        if any(
            e.source.connection == connection and same_table(e.source.table, p.table)
            for e in entities
        )
    }
    return mapped or {p.table for p in profiles}


def _probe(args: argparse.Namespace) -> int:
    """Materialize the warehouse's full profile into `profiles/<conn>.probe.json`.

    `introspect --profile` runs the same passes and PRINTS them — gone when the
    terminal scrolls, so every scaffolded project served with zero value
    dictionaries. This writes the artifact the project keeps: partitions, row
    counts, freshness, value dictionaries, crossed with the entities that read
    each table. Committed, it rides `dst apply` into the store the serving
    prompt reads, so generation filters on literals the warehouse actually holds
    ('FI') instead of guessing formats ('Finland'). Re-run freely — nightly cron
    is the intended cadence — and no server is needed.

    Without --connection, every warehouse connection dst.yaml declares is
    probed; one failing loudly does not stop the rest (exit 1 if any failed).

    SCALE. The catalog pass is metadata-only and always covers every table; the
    SAMPLING pass (one capped read per table) covers the tables the semantic
    layer reads — on a 5000-table warehouse the nightly cron pays for the
    governed scope, not the catalog. No layer yet means no scope, so everything
    samples (bootstrap mode, per-table caps still apply). `--sample-all` widens
    to every table; `--tables a,b` narrows the sampling to exactly those. What
    was NOT sampled is said out loud — a silent cap reads as coverage."""
    from services.project.compile import CompileError, dialect_for
    from services.project.loader import split_semantic
    from services.project.schema import parse_project_yaml
    from services.project.warehouse_drift import parse_layer

    root = Path(args.dir or ".")
    path = root / "dst.yaml"
    if not path.exists():
        print(f"error: no dst.yaml in {root} — probe reads connections from it", file=sys.stderr)
        return 1
    try:
        project = parse_project_yaml(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 1

    def _is_warehouse(conn_type: str) -> bool:
        try:
            dialect_for(conn_type)
        except CompileError:
            return False  # a context source — nothing to sample
        return True

    if args.connection:
        if args.connection not in project.connections:
            print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
            return 1
        wanted = [args.connection]
    else:
        wanted = [n for n, d in project.connections.items() if _is_warehouse(d.type)]
        if not wanted:
            print("error: dst.yaml declares no warehouse connection to probe", file=sys.stderr)
            return 1
    entities_by_path, _defs = parse_layer(split_semantic(_read_project(root)))
    entities = list(entities_by_path.values())
    # A table skipped by the sampling pass warns through its module logger
    # — mirror that to stderr so the operator sees it
    # here, not only in a server log (finding 4d's lesson).
    import logging as _logging

    from services.connectors import sampling as _sampling

    class _MirrorFormat(_logging.Formatter):
        # Progress lines (INFO) ride unprefixed so a long probe is visibly
        # alive; skips keep their warning: prefix.
        def format(self, record: _logging.LogRecord) -> str:
            prefix = "warning: " if record.levelno >= _logging.WARNING else ""
            return prefix + record.getMessage()

    mirror = _logging.StreamHandler(sys.stderr)
    mirror.setFormatter(_MirrorFormat())
    mirror.setLevel(_logging.INFO)
    prior_level = _sampling.logger.level
    _sampling.logger.setLevel(_logging.INFO)
    _sampling.logger.addHandler(mirror)
    try:
        return _probe_connections(args, root, wanted, entities)
    finally:
        _sampling.logger.removeHandler(mirror)
        _sampling.logger.setLevel(prior_level)


def _probe_connections(
    args: argparse.Namespace, root: Path, wanted: list[str], entities: list[Any]
) -> int:
    from services.lenses.connections import build_connector
    from services.lenses.profiler import sample_profiles
    from services.lenses.profiler_catalog import catalog_profiles
    from services.project import probe as probe_artifact
    from services.project import warehouse_drift as wd

    worst = 0
    for name in wanted:
        type_, config, secret = _declared_connection(args, name)
        try:
            connector = build_connector(type_, config, secret)
            # The semantic layer's tables are guaranteed into the catalog pass
            # A wide warehouse's listing cap must never starve
            # the tables every lens actually reads.
            needed = {
                e.source.table for e in entities if e.source.connection == name and e.source.table
            }
            profiles = catalog_profiles(connector, name, priority_tables=needed)
            if not profiles:
                # Empty output is never success (blocker 1): say so, exit non-zero.
                print(f"error: connection '{name}' listed no tables", file=sys.stderr)
                worst = 1
                continue
            scope = _sample_scope(args, profiles, entities, name)
            # Checkpoint BEFORE the slow pass: without it a wedged run discards
            # everything, leaving profiles/ empty and a nightly that produces
            # nothing indefinitely. The catalog artifact lands
            # first; sampling enriches it in place afterwards.
            probe_artifact.write_probe(root, name, profiles, entities)
            print(
                f"'{name}': catalog checkpoint written ({len(profiles)} tables) — "
                f"sampling {len(scope)} table(s) now",
                file=sys.stderr,
            )
            merged = sample_profiles(connector, [p for p in profiles if p.table in scope])
            profiles = [merged.get(p.table, p) for p in profiles]
        except Exception as exc:  # noqa: BLE001 — one dead warehouse must not stop the rest
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            print(f"error: connection '{name}' did not probe ({first})", file=sys.stderr)
            worst = 1
            continue
        if len(scope) < len(profiles):
            print(
                f"sampled {len(scope)} of {len(profiles)} table(s) — the semantic layer "
                f"reads {len(scope)}; catalog metadata covers the rest (--sample-all "
                "samples every table, --tables a,b exactly those)",
                file=sys.stderr,
            )
        out, artifact = probe_artifact.write_probe(root, name, profiles, entities)
        # One artifact family: probing ARMS drift too. The baseline is the
        # schema-only whitelist over the same tables the artifact holds, so a
        # project that ran `dst probe` — the documented first step — never sits in
        # the silent unarmed state `dst drift` now refuses (exit 4).
        wd.write_baseline(root, name, artifact.tables)
        # Counted off the artifact, so the summary describes the file on disk.
        complete = sum(
            1 for p in artifact.tables for c in p.columns if c.top_values and c.values_complete
        )
        partial = sum(
            1 for p in artifact.tables for c in p.columns if c.top_values and not c.values_complete
        )
        partitioned = sum(1 for p in artifact.tables if p.partitioning is not None)
        bits = [f"{complete} complete value dictionaries"]
        if partial:
            bits.append(f"{partial} partial")
        if partitioned:
            bits.append(f"{partitioned} partitioned table(s)")
        rel = out.relative_to(root) if out.is_relative_to(root) else out
        print(f"probed '{name}': {len(artifact.tables)} table(s) -> {rel} ({', '.join(bits)})")
    if worst == 0:
        print(
            "commit profiles/ — the next `dst apply` lands these facts in the serving prompt",
            file=sys.stderr,
        )
    return worst


def _import_metric_layer(args: argparse.Namespace) -> int:
    """Dispatch `dst import <what>`; each source owns its own required flags."""
    if args.what == "osi":
        return _import_osi(args)
    if not args.target_dir:
        print("error: --target-dir is required for `import dbt`", file=sys.stderr)
        return 1
    return _import_dbt(args)


def _import_osi(args: argparse.Namespace) -> int:
    """One-shot: an OSI/Ossie semantic model -> dst-owned semantic/ files.

    The direction that answers "why must I author this twice?". Relationships arrive
    with their cardinality already stated — the spec defines `from` as the many side —
    so the imported joins are the safe kind the compiler can emit, rather than the
    inferred kind that is wrong 8% of the time (measured on BIRD's foreign keys).
    """
    import json as _json

    import yaml

    from services.osi import from_osi
    from services.semantic.files import render_semantic_files

    if not args.file:
        print("error: --file is required for `import osi`", file=sys.stderr)
        return 1
    source = Path(args.file)
    if not source.exists():
        print(f"error: no such file: {source}", file=sys.stderr)
        return 1
    raw = source.read_text(encoding="utf-8")
    try:
        document = _json.loads(raw) if source.suffix == ".json" else yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 — a malformed file is a message, not a stack
        print(f"error: could not parse {source}: {exc}", file=sys.stderr)
        return 1
    try:
        result = from_osi(document, connection=args.connection)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    files = render_semantic_files(result.entities, [])
    header = f"# imported from OSI model {source.name} - dst-owned; not re-synced\n"
    root = Path(args.dir)
    for path, content in files.items():
        out = root / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            (header + content) if path.endswith((".yaml", ".yml")) else content, encoding="utf-8"
        )

    joins = sum(len(e.joins) for e in result.entities)
    metrics = sum(len(e.metrics) for e in result.entities)
    print(f"imported {len(result.entities)} entities, {joins} joins, {metrics} metrics")
    if result.ai_context:
        # It belongs on the lens, which this command does not write — say so rather
        # than drop the one field the spec reserves for exactly our kind of judgment.
        print(
            "\nthe model's ai_context did not come across (it belongs on a lens, not an "
            f"entity) — paste it into your lens's `instructions`:\n  {result.ai_context[:300]}"
        )
    if result.skipped:
        print(f"\nnot imported ({len(result.skipped)}):")
        for reason in result.skipped:
            print(f"  - {reason}")
    print(f"\nwrote {len(files)} files under {root}/semantic/ - review, then `dst apply`")
    return 0


def _export_osi(args: argparse.Namespace) -> int:
    """dst's shared semantic layer -> an OSI/Ossie semantic model.

    Answers "what happens when I leave?" — and fills the spec's `ai_context` slots,
    which is where dst's grain/use-cases/definitions belong and where a plain
    schema export has nothing to say.
    """
    import yaml

    from services.osi import to_osi
    from services.project.loader import split_semantic
    from services.semantic.files import parse_semantic_files

    root = Path(args.dir)
    entities, definitions = parse_semantic_files(split_semantic(_read_project(root)))
    if not entities:
        print(f"error: no semantic/entities/*.yaml under {root}", file=sys.stderr)
        return 1
    document, skipped = to_osi(
        list(entities.values()),
        name=args.name or root.resolve().name,
        dialect=args.dialect,
        definitions=list(definitions.values()),
    )
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    if skipped:
        print(f"\nnot exported ({len(skipped)}):", file=sys.stderr)
        for reason in skipped:
            print(f"  - {reason}", file=sys.stderr)
    return 0


def _import_dbt(args: argparse.Namespace) -> int:
    """One-shot: dbt artifacts -> dst-owned semantic/ files. Never re-synced."""
    from services.dbt import import_shared_assets, load_artifacts
    from services.dbt.report import coverage_report, render_text
    from services.semantic.files import render_semantic_files

    artifacts = load_artifacts(Path(args.target_dir))
    result = import_shared_assets(artifacts, connection=args.connection)
    files = render_semantic_files(result.entities, result.definitions)
    header = (
        f"# imported from dbt project '{artifacts.project}' (manifest "
        f"{artifacts.manifest_version}) - dst-owned; not re-synced\n"
    )
    root = Path(args.dir)
    for path, content in files.items():
        out = root / path
        out.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith((".yaml", ".yml")):
            content = header + content
        out.write_text(content, encoding="utf-8")
    print(render_text(coverage_report(artifacts, result)))
    print(f"wrote {len(files)} files under {root}/semantic/ - review, then `dst apply`")
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


def _serve(args: argparse.Namespace) -> int:
    import os
    import subprocess
    from urllib.parse import urlparse

    import uvicorn

    from services.config import resolve_env_ref, settings
    from services.db.schema_state import schema_state, serve_refusal

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
    uvicorn.run("services.app:app", host=args.host, port=args.port, reload=args.reload)
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
    _migrate(args)
    return _serve(args)


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
    if env_key and not admin_first:
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


def _connections_snippet(connections: list[dict[str, object]]) -> str:
    """DB connections as a dst.yaml SNIPPET — printed, never written: the
    user's dst.yaml is authored (comments, env refs) and stays theirs.
    Secrets never leave the server; a stored one renders as the secret_env
    line to fill (the init convention: DST_API_KEY_<NAME>)."""
    import yaml

    lines = ["connections:"]
    for c in connections:
        lines.append(f"  {c['name']}:")
        lines.append(f"    type: {c['type']}")
        if c.get("config"):
            flow = yaml.safe_dump(c["config"], default_flow_style=True).strip()
            lines.append(f"    config: {flow}")
        if c.get("has_secret"):
            env = "DST_API_KEY_" + str(c["name"]).upper().replace("-", "_")
            lines.append(f"    secret_env: {env}  # secret stays server-side; set this in .env")
    return "\n".join(lines)


def _comment_lines(text: str) -> list[str]:
    """The whole-line YAML comments in a file. Trailing comments ride on a line
    that also carries data, so they are counted with it rather than alone; a
    definition page's `#` is a markdown heading, never a comment, which is why
    only YAML is asked."""
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#")]


def _refuses_comment_loss(
    args: argparse.Namespace,
    project: dict[str, str],
    files: dict[str, str],
    moved: dict[str, str],
) -> bool:
    """Name the comments this export is about to drop, BEFORE it drops them —
    and take y/N on it (`--yes` headless), the `dst lens rm` idiom.

    Export rewrites files from server state, and the server stores no comments:
    the provenance header of a certified_answers.yaml and the inline `# 200
    truncated a 2000-row answer` note in a lens.yaml both vanished, silently, on
    a command whose job is to be a safe round-trip. The loss is unavoidable —
    those bytes were never sent — so consent is the honest half. Nothing to lose
    (a fresh checkout, an uncommented tree) still asks nothing."""
    losing = {}
    for path, content in files.items():
        dest = moved.get(path, path)
        if not dest.endswith((".yaml", ".yml")):
            continue
        old = project.get(dest)
        if old is None:
            continue
        keeping = set(_comment_lines(content))
        lost = [c for c in _comment_lines(old) if c not in keeping]
        if lost:
            losing[dest] = lost
    if not losing:
        return False
    print(
        "these files are rewritten from server state, and the server stores no "
        "comments — export will drop:"
    )
    for dest, lost in sorted(losing.items()):
        print(f"  {dest}: {len(lost)} comment line(s), e.g. {lost[0][:70]}")
    if args.yes:
        return False
    if not sys.stdin.isatty():
        print("error: refusing without confirmation — pass --yes", file=sys.stderr)
        return True
    if input("overwrite? y/N: ").strip().lower() != "y":
        print("aborted — nothing written")
        return True
    return False


def _export(args: argparse.Namespace) -> int:
    if getattr(args, "what", None) == "osi":
        return _export_osi(args)
    import httpx

    url, headers = _client(args)
    params = [("lens", name) for name in (args.lens or [])]
    r = httpx.get(f"{url}/mgmt/project/export", headers=headers, params=params, timeout=60)
    if r.status_code == 404:
        print(f"error: {r.json()['detail']}", file=sys.stderr)
        return 1
    from services.project.loader import existing_asset_paths

    out = r.raise_for_status().json()
    files = out["files"]
    # --out redirects the whole tree — the safe path (export elsewhere, diff,
    # then adopt). It used to be osi-only and silently ignored here, so the
    # flag that meant "write output here" overwrote the project instead
    # . All guards below run against the destination.
    root = Path(args.out) if args.out else Path(args.dir)
    project = _read_project(root)
    # The server renders every asset to its canonical path; this project may
    # already author it somewhere else (a folder, or the term's own spelling —
    # `customer_nodes.yaml`, not the slug's `customer-nodes.yaml`). Writing the
    # canonical path anyway left a SECOND page per asset and the next `dst
    # plan` exited 1 on a tree export itself had just written.
    moved = existing_asset_paths(project, files)
    if _refuses_comment_loss(args, project, files, moved):
        return 1
    import yaml

    for path, content in files.items():
        dest = root / moved.get(path, path)
        if (
            path.endswith(("certified_answers.yaml", "cases.yaml"))
            and not dest.exists()
            and not yaml.safe_load(content)
        ):
            # Never stub an empty list file into being: files win on apply, so a
            # `[]` this export creates would delete server-side answers on a
            # later, unrelated apply.
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    for path, here in sorted(moved.items()):
        print(f"{path} -> {here} (this project already authors that asset there)")
    print(f"exported {len(files)} files to {root.resolve()}/")
    if out.get("connections"):
        print("\n# server connections — merge into dst.yaml yourself (never auto-written):")
        print(_connections_snippet(out["connections"]))
    # Adoption summary: from here on the files govern these lenses. Absolute
    # paths — a relative `wrote lenses/...` in scrollback reads as "it went
    # where you asked" even when it didn't.
    for name in sorted({p.split("/")[1] for p in files if p.startswith("lenses/")}):
        print(f"wrote {root.resolve()}/lenses/{name}/ — commit it; future applies now govern it")
    return 0


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
    return files


def _project_files(args: argparse.Namespace, verb: str) -> dict[str, str]:
    """The project under --dir — or a named refusal, before any request.

    A directory with no project used to be indistinguishable from a project
    with no changes: `_read_project` returned {}, the server answered `[]`, and
    plan and apply both exited 0 with nothing to show. So a mistyped --dir, or a
    shell one level out of the project, reads as "it worked": an agent can take
    `exit 0` + `[]` as proof its correction landed. No dst.yaml and
    no lenses/ or semantic/ files is not an empty project, it is no project."""
    root = Path(getattr(args, "dir", ".") or ".")
    files = _read_project(root)
    if not files:
        print(
            f"error: no dst project in {root.resolve()} — no dst.yaml, and no "
            f"lenses/ or semantic/ files, so there is nothing to {verb}. Run `dst init` "
            "to create one here, or name the project with --dir PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "dst.yaml" not in files:
        # A real tree can lack one — `dst export` writes assets and never
        # authors dst.yaml — but then connections and providers are not in
        # this push, and a silent omission reads as a push that covered them.
        print(
            f"warning: no dst.yaml in {root.resolve()} — {len(files)} asset file(s) "
            f"in this {verb}; connections and providers are NOT part of it",
            file=sys.stderr,
        )
    return files


def _warehouse_probe(args: argparse.Namespace, connection: str) -> dict[str, object]:
    """One `scope: warehouse` plan entry: has the warehouse moved since profiling?

    Deliberately the cheap half of `dst drift` — a catalog pass and a count.
    It does not load `semantic/`, does not cross-reference and does not format, so
    what plan pays is one metadata round-trip regardless of project size."""
    from services.lenses.connections import build_connector
    from services.lenses.profiler_catalog import catalog_profiles
    from services.project import warehouse_drift as wd

    root = Path(getattr(args, "dir", ".") or ".")
    entry: dict[str, object] = {"scope": "warehouse", "connection": connection, "drift": "armed"}
    baseline = wd.read_baseline(root, connection)
    if baseline is None:  # slugged to this file under another name
        return {
            **entry,
            "drift": "unarmed",
            "status": "unavailable",
            "note": "no baseline recorded",
        }
    entry["baseline_recorded_at"] = baseline.recorded_at.isoformat()
    type_, config, secret = _declared_connection(args, connection)
    if not type_:
        return {
            **entry,
            "status": "unavailable",
            "note": f"'{connection}' has a recorded baseline but is not declared in dst.yaml",
        }
    try:
        current = catalog_profiles(build_connector(type_, config, secret), connection)
    except Exception as exc:  # noqa: BLE001 — a courtesy check never fails a plan
        first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        return {**entry, "status": "unavailable", "note": f"did not profile from here ({first})"}
    deltas = wd.schema_delta_count(baseline.tables, current)
    return {
        **entry,
        "status": "changed" if deltas else "unchanged",
        "deltas": deltas,
        "note": (
            f"the warehouse has changed since profiling ({deltas} schema delta(s)) — "
            "run `dst drift`"
        ),
    }


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


def _warehouse_entries(args: argparse.Namespace) -> list[dict[str, object]]:
    """The staleness check, bounded twice over — plus the standing armed line.

    Every warehouse connection dst.yaml declares gets a drift entry: armed ones
    (a recorded baseline) pay one metadata round-trip for the staleness check;
    UNARMED ones cost nothing and say so — the silent unarmed state is the
    failure `dst drift`'s exit 4 exists for, and plan is where it stops being
    silent. Second bound: the whole probe runs on a daemon thread with
    one deadline, so a warehouse that hangs costs `PLAN_WAREHOUSE_TIMEOUT` and
    then plan carries on. Degradation is silence on the human path and a
    `status: unavailable` entry under --json — never an error, because the
    warehouse being unreachable from this laptop says nothing about whether the
    project is valid, which is what plan answers."""
    import threading

    from services.project import warehouse_drift as wd

    root = Path(getattr(args, "dir", ".") or ".")
    armed = wd.baseline_connections(root)
    declared = _declared_warehouses(root)
    # No dst.yaml (an export-only tree): the recorded baselines are all we know.
    unarmed = [n for n in declared or [] if n not in armed]
    connections = armed
    out: list[dict[str, object]] = [
        {
            "scope": "warehouse",
            "connection": name,
            "drift": "unarmed",
            "status": "unarmed",
            "note": "drift UNARMED — run `dst probe` to record the baseline",
        }
        for name in unarmed
    ]
    if not connections:
        return out

    def probe() -> None:
        for name in connections:
            out.append(_warehouse_probe(args, name))

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(PLAN_WAREHOUSE_TIMEOUT)
    if worker.is_alive():
        # One snapshot, because the thread we gave up on is still appending to it.
        out = list(out)
        done = {str(e.get("connection")) for e in out}
        return out + [
            {
                "scope": "warehouse",
                "connection": name,
                "status": "unavailable",
                "note": f"no answer within {PLAN_WAREHOUSE_TIMEOUT:g}s — skipped",
            }
            for name in connections
            if name not in done
        ]
    return out


def _render_plan(plan: list[dict[str, Any]], write: Callable[[str], None]) -> int:
    """Print the server's plan; return how many files apply would reject.

    Split out from `_plan` so `--json` can reuse the counting without the prose —
    two implementations of "would apply reject this?" is one too many."""
    invalid = 0
    for entry in plan:
        if entry.get("scope") == "server_only":
            write(f"server-only: {entry['note']}")
            continue
        if entry.get("scope") == "unchecked":
            # A clean plan is not a clean bill of health — say what it could not
            # check, or a green exit 0 reads as one (regression sweep, finding 2).
            write("not checked by plan — apply still runs these, and they need a live warehouse")
            for check in entry["checks"]:
                write(f"  - {check}")
            continue
        if entry.get("scope") == "project":
            invalid += 1
            write(f"dst.yaml: invalid — {entry['error']}")
            continue
        if entry.get("scope") == "semantic":
            if entry.get("status") == "hint":
                write(f"hint: {entry['hint']}")
            elif entry.get("status") == "orphan":
                write(f"orphan: {entry['note']}")
            elif entry.get("error"):
                invalid += 1
                where = f"{entry['path']}: " if entry.get("path") else "semantic: "
                write(f"{where}invalid — {entry['error']}")
            else:
                write(f"{entry['path']}: {entry['status']}")
                if entry.get("diff"):
                    write(entry["diff"])
            continue
        errors = entry.get("errors") or ([entry["error"]] if entry.get("error") else [])
        if entry.get("status") == "invalid":
            invalid += 1
        # One error still reads on the status line; a lens rejected on several
        # gates at once gets one per line — apply prints them that way, and a
        # semicolon-joined wall of five is what people stop reading.
        note = f" — {errors[0]}" if len(errors) == 1 else ""
        if entry.get("stale"):
            note += (
                f" — STALE compile (shared changed: {', '.join(entry['stale'])})"
                " → will recompile on apply"
            )
        write(f"{entry['lens']}: {entry['status']}{note}")
        if len(errors) > 1:
            for message in errors:
                write(f"  - {message}")
        # A behaviour change with no file behind it: `!` marks the one line the
        # rest of a plan cannot express — nothing here is a diff to apply.
        for line in entry.get("semantics", []):
            write(f"  ! {line}")
        if entry.get("certified"):  # re-verify is a human act — the plan only names them
            write(f"  {entry['certified']['note']}")
            for q in entry["certified"]["questions"]:
                write(f"    re-verify: {q}")
        for d in entry.get("diffs", []):
            write(d["diff"] or f"  + {d['path']}")
    return invalid


_PLAN_GLYPHS = {
    "create": ("+", style.good),
    "update": ("~", style.warn),
    "invalid": ("✗", style.bad),
    "stale": ("!", style.warn),
    "certified-stale": ("!", style.warn),
    "orphan": ("-", style.bad),
}


def _summarize_plan(plan: list[dict[str, Any]]) -> None:
    """The terraform-shaped default: one row per asset that needs attention,
    then the counts line — diffs ride behind --full. A ~2k-line full plan was
    the daily read; the summary is the read, --full the review."""
    counts: dict[str, int] = {}
    rows: list[str] = []
    hints = 0

    def row(status: str, name: str, note: str = "") -> None:
        glyph, paint = _PLAN_GLYPHS.get(status, ("~", style.warn))
        head = paint(glyph) + f" {name}"
        rows.append(f"  {head}" + (f" — {note}" if note else ""))

    for entry in plan:
        if entry.get("scope") == "server_only":
            # Verbatim, not a glyph row: the note IS the adoption pointer.
            rows.append(f"server-only: {entry.get('note')}")
            continue
        if entry.get("scope") == "unchecked":
            continue  # printed verbatim below the rows — it is a guarantee statement
        if entry.get("scope") == "project":
            counts["invalid"] = counts.get("invalid", 0) + 1
            row("invalid", "dst.yaml", str(entry.get("error") or ""))
            continue
        if entry.get("scope") == "semantic":
            status = str(entry.get("status") or "")
            if status == "hint":
                hints += 1
                continue
            if status == "orphan":
                row("orphan", str(entry.get("note") or ""))
                continue
            if entry.get("error"):
                counts["invalid"] = counts.get("invalid", 0) + 1
                path = str(entry.get("path") or "semantic")
                note = str(entry["error"]).removeprefix(f"{path}: ")
                row("invalid", path, note)
                continue
            counts[status] = counts.get(status, 0) + 1
            if status != "unchanged":
                row(status, str(entry.get("path") or ""))
            continue
        # lens rows
        status = str(entry.get("status") or "")
        errors = entry.get("errors") or ([entry["error"]] if entry.get("error") else [])
        if status == "invalid":
            counts["invalid"] = counts.get("invalid", 0) + 1
            row("invalid", str(entry.get("lens") or ""), str(errors[0]) if errors else "invalid")
            for message in errors[1:]:
                rows.append(f"      {message}")
            continue
        counts[status] = counts.get(status, 0) + 1
        note = ""
        if entry.get("stale"):
            note = f"STALE compile (shared changed: {', '.join(entry['stale'])})"
        if status != "unchanged" or note:
            row(entry.get("status") or "update", str(entry.get("lens") or ""), note or status)
        for line in entry.get("semantics", []):
            rows.append("  " + style.warn("!") + f" {line}")
        if entry.get("certified"):
            n = len(entry["certified"].get("questions") or [])
            rows.append(
                "  " + style.warn("!") + f" {entry.get('lens')}: {n} certified answer(s) "
                "need re-verify (--full lists them)"
            )

    for line in rows:
        print(line)
    if rows:
        print()
    for entry in plan:  # the unchecked block survives summarization verbatim
        if entry.get("scope") == "unchecked":
            print("not checked by plan — apply still runs these, and they need a live warehouse")
            for check in entry["checks"]:
                print(f"  - {check}")
    parts = []
    for key, label in (
        ("create", "to add"),
        ("update", "to change"),
        ("invalid", "invalid"),
        ("stale", "stale"),
        ("unchanged", "unchanged"),
    ):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    if hints:
        parts.append(f"{hints} hint(s)")
    summary = "Plan: " + (", ".join(parts) if parts else "nothing to do") + "."
    print(style.bold(summary), style.dim("(--full shows diffs and hints)"))


def _plan(args: argparse.Namespace) -> int:
    """Diff the project against the server — and say when the WAREHOUSE moved too.

    plan is the verb an engineer runs every day, which is the whole reason the
    staleness line lives here: on the morning the warehouse gains the column that
    invalidates a definition, `introspect`, `plan` and `test` are all structurally
    incapable of mentioning it. The line is a pointer, not the analysis —
    `dst drift` does the analysis."""
    import httpx

    body = {"files": _project_files(args, "plan")}
    url, headers = _client(args)
    try:
        r = httpx.post(f"{url}/mgmt/project/plan", headers=headers, json=body, timeout=args.timeout)
    except httpx.TimeoutException:
        # A raw ReadTimeout traceback tells the operator nothing.
        # plan takes no lock and writes nothing — say so, and name the knob.
        print(
            f"error: no response from {url} within {args.timeout}s — plan is read-only "
            "and changed nothing. A large project or a cold server needs longer: "
            "retry with --timeout SECONDS.",
            file=sys.stderr,
        )
        return 1
    plan = r.raise_for_status().json()
    warehouse = _warehouse_entries(args)
    invalid = _render_plan(plan, lambda _line: None)  # the one count, however rendered
    if args.as_json:
        print(json.dumps(plan + warehouse, indent=2, ensure_ascii=False))
    elif getattr(args, "full", False):
        _render_plan(plan, _print_plan_line)
    else:
        _summarize_plan(plan)
    if not args.as_json:
        # The standing drift line, one per declared warehouse connection: armed
        # with its baseline date, or UNARMED loudly — the silent unarmed state
        # is exactly what this line retires. Staleness stays quieter:
        # only `changed` earns its extra line; `unchanged` is the daily case and
        # `unavailable` is a degradation of a courtesy — readable under --json,
        # which is where a caller that needs to know goes looking.
        for entry in warehouse:
            name = entry.get("connection")
            if entry.get("drift") == "unarmed":
                print(style.warn(f"drift: '{name}' UNARMED — run `dst probe`"))
            else:
                date = str(entry.get("baseline_recorded_at") or "")[:10]
                print(style.dim(f"drift: '{name}' armed (baseline {date})"))
            if entry.get("status") == "changed":
                print(style.accent("warehouse:") + f" '{entry['connection']}' — {entry['note']}")
        # The scaffolded skills are the author's files, so plan has no server
        # view of them — but plan is where an author is, and an init-time
        # snapshot that silently outlives its release is the defect (#51).
        # Dim, one line, and only when something actually differs.
        from services.cli.init import stale_agent_note

        if note := stale_agent_note(Path(getattr(args, "dir", ".") or ".")):
            print(style.dim(note))
    if invalid:
        # A plan that predicts a rejected apply must not exit 0 — the one line
        # saying so scrolled past under a screenful of clean create-diffs, and
        # the agent reading the exit code went straight to apply (blocker 5).
        print(
            style.bad(
                f"error: {invalid} file(s) would be REJECTED by apply — fix them above; "
                "nothing was changed",
                sys.stderr,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


def _print_plan_line(line: str) -> None:
    """--full's writer: today's byte layout, with diff/verdict lines painted."""
    if "\n" in line:
        print("\n".join(style.diff_line(part) for part in line.splitlines()))
    elif ": invalid — " in line or line.startswith("dst.yaml: invalid"):
        print(style.bad(line))
    elif "STALE compile" in line:
        print(style.warn(line))
    else:
        print(line)


_APPLY_IN_FLIGHT = (
    "the server may still be applying — it holds the org apply lock and will commit "
    "when done. Do not re-apply until it does (a second apply gets 409 while the "
    "lock is held). Check liveness with `curl <server>/ready` FIRST: if /ready "
    "does not answer within seconds, the server is wedged, not working — restart "
    "it (a dead warehouse socket can hang the whole process; "
    "`dst plan` polls the SAME server, so it cannot distinguish the two states)."
)


def _summarize_apply(out: list[dict[str, Any]], quiet: bool = False) -> None:
    """The terraform-shaped apply report: grouped sections, warnings and errors
    painted, one counts line — the raw row array (agents' shape) is --json.
    Rendering is defensive on purpose: server rows are an open dict contract,
    and an unknown row must degrade to its scope name, never crash the CLI.

    ``quiet`` keeps only what needs a human: rejected lenses,
    errors, deletions, each DISTINCT warning once with who it fired on, the
    counts line and the gate footer — a routine apply over a few dozen lenses
    prints hundreds of warning lines, and the noise floor is where a genuinely
    new one hides."""
    published = rejected = warnings_n = errors_n = 0
    # The same sentence repeated once per lens buries the two lines that matter:
    # most of a big apply's warnings are one warning fired many times, and the
    # single connection-probe diagnostic scrolls off the top. A warning identical
    # across 3+ rows prints once, counted; --json keeps every row verbatim.
    warn_counts: dict[str, int] = {}
    warn_where: dict[str, list[str]] = {}
    for entry in out:
        row_name = str(entry.get("lens") or entry.get("scope") or "")
        for note in entry.get("warnings") or []:
            warn_counts[str(note)] = warn_counts.get(str(note), 0) + 1
            warn_where.setdefault(str(note), []).append(row_name)
    deduped: set[str] = set()
    for entry in out:
        if entry.get("scope") == "apply":
            continue  # the abort row becomes the banner below
        head = str(entry.get("lens") or entry.get("scope") or "")
        action = str(entry.get("action") or "")
        version = entry.get("version")
        label = style.accent(f"lens {head}" if entry.get("lens") else head)
        loud = bool(entry.get("errors")) or action.startswith("rejected")
        if entry.get("lens"):
            verdict = action or "unchanged"
            if action == "published":
                published += 1
                verdict = style.good(f"published v{version}" if version is not None else action)
            elif action == "rejected":
                rejected += 1
                verdict = style.bad(action)
            if not quiet or loud:
                print(f"{label}: {verdict}")
            # A deletion is the one staged count that must not be summarized
            # away: files-win removal of a certified answer printed only
            # "updated", and the operator's sole evidence lived in --json.
            # The server emits "deleted N" only when N > 0, so this is quiet
            # on every non-deleting apply — and it survives --quiet.
            for a in entry.get("applied") or []:
                if "deleted" in str(a):
                    if quiet and not loud:
                        print(f"{label}: {a}")
                    else:
                        print(f"  {a}")
        elif not quiet or loud:
            detail = action or ", ".join(str(a) for a in entry.get("applied") or []) or "ok"
            print(f"{label}: {detail}")
        if not quiet:
            for cap in entry.get("capabilities") or []:
                print(f"  {cap}")
        for note in entry.get("warnings") or []:
            warnings_n += 1
            if quiet:
                # Every distinct warning exactly once, naming who it fired on —
                # counted here, printed after the loop so order is stable.
                continue
            if warn_counts.get(str(note), 0) >= 3:
                if str(note) in deduped:
                    continue
                deduped.add(str(note))
                times = warn_counts[str(note)]
                print(
                    f"  {style.warn('warning:')} {note} "
                    + style.dim(f"(identical on {times} rows — --json for each)")
                )
                continue
            print(f"  {style.warn('warning:')} {note}")
        for err in entry.get("errors") or []:
            errors_n += 1
            print(f"  {style.bad('error:')} {err}")
    if quiet:
        for note, rows in warn_where.items():
            where = ", ".join(rows[:3]) + (f", +{len(rows) - 3} more" if len(rows) > 3 else "")
            print(f"{style.warn('warning:')} {note} {style.dim(f'({where})')}")
    bits = []
    if published:
        bits.append(f"{published} lens(es) published")
    if rejected:
        bits.append(f"{rejected} rejected")
    if warnings_n:
        bits.append(f"{warnings_n} warning(s)")
    bits.append(f"{errors_n} error(s)")
    line = "Apply complete. " if not (errors_n or rejected) else "Apply finished. "
    paint = style.good if not (errors_n or rejected) else style.bad
    print(paint(line.strip()), ", ".join(bits) + ".", style.dim("(--json for the row array)"))
    # One line for the safety net: 40 identical skip
    # warnings above are unreadable; whether the gates actually ran must be
    # legible without grepping. Skips break out by reason — "empty suite" is
    # benign, "provider error" means the scorer broke.
    gate_counts: dict[str, int] = {}
    for entry in out:
        g = entry.get("gate")
        if entry.get("lens") and g:
            gate_counts[str(g)] = gate_counts.get(str(g), 0) + 1
    if gate_counts:
        parts = [
            f"{n} {outcome}"
            for outcome, n in sorted(gate_counts.items())
            if not outcome.startswith("skipped")
        ]
        skipped = {k: n for k, n in gate_counts.items() if k.startswith("skipped")}
        if skipped:
            reasons = ", ".join(
                f"{n} {k[len('skipped (') : -1]}" for k, n in sorted(skipped.items())
            )
            parts.append(f"{sum(skipped.values())} skipped ({reasons})")
        gate_line = f"eval gates: {', '.join(parts)}"
        print(style.warn(gate_line) if skipped else gate_line)


def _apply(args: argparse.Namespace) -> int:
    import httpx

    body = {"files": _project_files(args, "apply")}
    url, headers = _client(args)
    params: dict[str, str] = {}
    if getattr(args, "probe_certified", False):
        params["probe_certified"] = "true"
    if getattr(args, "require_gates", False):
        params["require_gates"] = "true"
    if getattr(args, "allow_failing_cases", False):
        reason = (getattr(args, "reason", None) or "").strip()
        if not reason:
            print(
                "error: --allow-failing-cases needs --reason '…' — the audited one-off "
                "must say why (e.g. 'intended behaviour change: term now ambiguous')",
                file=sys.stderr,
            )
            return 2
        params["allow_failing_cases"] = "true"
        params["override_reason"] = reason
    try:
        r = httpx.post(
            f"{url}/mgmt/project/apply",
            headers=headers,
            params=params or None,
            json=body,
            timeout=args.timeout,
        )
    except httpx.TimeoutException:
        # The zombie apply: the handler is sync `def`, so the client
        # disconnect CANNOT cancel it — the apply keeps running in the
        # threadpool and commits minutes after the CLI gave up. A raw httpx
        # traceback here made the operator believe nothing landed and retry
        # into a 409. Name what the server is still doing instead.
        print(
            f"error: no response from {url} within {args.timeout}s — {_APPLY_IN_FLIGHT} "
            "Raise --timeout SECONDS to wait longer. What costs the time is the "
            "certify self-test: one full generation per certified answer the push "
            "lands (~30s each measured, bounded by DST_CERTIFY_SELFTEST_BUDGET_S — read "
            "that budget in ANSWERS, not seconds: the 120s default covered 4 of a "
            "7-answer push), plus the eval gate when eval_gate is warn/block.",
            file=sys.stderr,
        )
        return 1
    if r.status_code in (502, 503, 504):
        # A proxy's verdict, not the server's: upstream never answered, so the
        # apply may be running still. The one 5xx family that must NOT claim
        # "nothing was deployed" (the incident that taught this arrived as a 502).
        print(
            f"error: apply got HTTP {r.status_code} from a gateway, not from dst — "
            f"{_APPLY_IN_FLIGHT}",
            file=sys.stderr,
        )
        return 1
    if r.status_code == 409:  # another apply holds the per-org lock
        # The holder is a live apply, not a stuck one — it will
        # commit. "Retry shortly" alone let the operator read the lock as
        # wreckage from the run they thought had failed.
        print(
            f"error: {r.json()['detail']} — that apply is still running and will "
            "commit when it finishes; `dst plan` shows committed state.",
            file=sys.stderr,
        )
        return 1
    if r.status_code >= 400:
        # A server error must name itself, not become a raw traceback: a 500's
        # actual cause is only in the server's log, so say where to look. It
        # said "check serve.log", which nothing ever writes: the
        # server logs to the TERMINAL running it, so name that instead.
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = None
        where = "the server's log (locally: the terminal running `dst dev`/`serve`)"
        # Apply has been read as non-atomic ("half of it is live"). Measured: it is
        # not — semantic assets, lens publishes, eval cases and certified
        # upserts share ONE request transaction, and an unhandled error rolls it
        # all back (tests/test_certified_gates.py). Say so: the operator was
        # left guessing which stages survived, and guessed wrong.
        print(
            f"error: apply failed (HTTP {r.status_code}): "
            f"{detail or r.text[:300] or 'no detail'} — nothing was deployed "
            "(one transaction: certified answers, eval cases and the lens publish "
            f"rolled back together) — full traceback in {where}",
            file=sys.stderr,
        )
        return 1
    out = r.json()
    if getattr(args, "as_json", False):
        # ensure_ascii=False: server rows carry prose warnings (em dashes, quotes);
        # \uXXXX escapes would make the one surface a CLI user reads unreadable.
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _summarize_apply(out, quiet=bool(getattr(args, "quiet", False)))
    abort = next(
        (e for e in out if e.get("scope") == "apply" and e.get("action") == "aborted"), None
    )
    if abort is not None:
        # Blue/green: the server rolled the whole apply back — say it loudly,
        # with every error, after the report.
        print(style.bad(f"\nAPPLY ABORTED — {abort['detail']}", sys.stderr), file=sys.stderr)
        for entry in out:
            for err in entry.get("errors") or []:
                name = entry.get("lens") or entry.get("scope")
                print("  " + style.bad(f"{name}: {err}", sys.stderr), file=sys.stderr)
        return 1
    # Belt for older servers: non-zero on ANY error, not just rejected lenses —
    # a bulk certified import where every entry hit a gate must not look green
    # to CI/agents.
    ok = all(r.get("action") != "rejected" and not r.get("errors") for r in out)
    return 0 if ok else 1


def _semantic(args: argparse.Namespace) -> int:
    """`dst semantic get|rm <kind> <name>` — read back or delete one shared
    asset. `get` prints the stored body as YAML, so confirming a declaration
    landed needs no direct database query; `rm` is the explicit
    deletion act plan's orphan listing points at."""
    import httpx

    url, headers = _client(args)
    if args.action == "get":
        r = httpx.get(f"{url}/mgmt/semantic/{args.kind}/{args.name}", headers=headers, timeout=60)
        if r.status_code == 404:
            print(f"no {args.kind} '{args.name}'", file=sys.stderr)
            return 1
        r.raise_for_status()
        body = r.json().get("body") or {}
        if getattr(args, "as_json", False):
            print(json.dumps(body, indent=2, ensure_ascii=False))
        else:
            import yaml

            print(yaml.safe_dump(body, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    r = httpx.delete(f"{url}/mgmt/semantic/{args.kind}/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no {args.kind} '{args.name}'", file=sys.stderr)
        return 1
    if r.status_code == 409:  # a published lens still selects it — server names them
        print(f"error: {r.json()['detail']}", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(f"removed {args.kind} '{args.name}'")
    return 0


def _prompt_preview(args: argparse.Namespace) -> int:
    """`dst lens prompt <lens> "<question>"` — what the model actually sees.

    The serialized semantic model had exactly one caller, inside the generation
    call: authors could only learn that a `dimensions:` block never reached the
    prompt by running a 15-25 minute benchmark lap (four agents did, and one
    spent three laps tuning a lens that was below its bare-schema control). This
    prints the assembled prompt and, per authored asset, whether it got there.
    No LLM call, no warehouse call."""
    import httpx

    if not args.question:
        print(
            'error: `dst lens prompt` needs a question — dst lens prompt <lens> "..."',
            file=sys.stderr,
        )
        return 1
    url, headers = _client(args)
    r = httpx.get(
        f"{url}/mgmt/lenses/{args.name}/prompt",
        headers=headers,
        params={"q": args.question},
        timeout=120,
    )
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    d = r.json()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    print(
        f"lens {d['lens']} · tier {d['generator_tier']} · prompt-set {d['prompt_hash']} "
        "· assembled, no LLM call"
    )
    if d.get("intent_system"):
        # The metric-layer tier answers FIRST; the raw-SQL prompt below is only
        # what an escalation would see. Printing them in the other order would
        # show the author a prompt the model may never be sent.
        print("\n=== first pass: metric-layer prompt ===")
        print(d["intent_system"])
        print("\n=== escalation: raw-SQL prompt ===")
    else:
        print("\n=== system prompt ===")
    print(d["system"])
    print(
        "\n=== context (user turn) === " + " · ".join(f"{k}: {v}" for k, v in d["counts"].items())
    )
    for chunk in d["prose"]:
        head = " ".join(chunk["text"].split())[:160]
        score = f" {chunk['score']:.2f}" if chunk["score"] else ""
        print(f"[{chunk['source']}]{score} ({len(chunk['text'])} chars) {head}")
    assets = d["assets"]
    kinds = dict.fromkeys(a["kind"] for a in assets)
    print("\n=== reaching the model ===")
    for kind in kinds:
        same = [a for a in assets if a["kind"] == kind]
        print(f"{kind:<16} {sum(1 for a in same if a['in_prompt'])}/{len(same)}")
    absent = [a for a in assets if not a["in_prompt"]]
    if absent:
        print("\n=== NOT reaching the model ===")
        for a in absent:
            print(f"{a['kind']:<16} {a['name']} — {a['note']}")
    late = [a for a in assets if a["in_prompt"] and a["note"]]
    if late:
        # Grouped: on a metric-layer lens this is most of the model, and the one
        # line that matters (the lens's own instructions) must not be buried.
        print("\n=== reaching it only if generation escalates to the raw-SQL tier ===")
        for kind in kinds:
            names = [a["name"] for a in late if a["kind"] == kind]
            shown = " · ".join(names) if len(names) <= 3 else ""
            if names:
                print(f"{kind:<16} {len(names):<4} {shown}")
    # The SECOND model call. Everything above decides the SQL; this decides the
    # English. Printing only the first half is how a certified answer came to
    # call a `'-'` "a null or placeholder value" with nothing able to show why.
    if d.get("compose_system"):
        print("\n=== second call: compose prompt (writes the English) ===")
        print(d["compose_system"])
        print(
            "\n=== compose (user turn) === <…> are slots — no generator and no "
            "warehouse ran; every column-bound definition is shown, a real serve "
            "carries the ones its answer's columns bind"
        )
        print(d["compose_user"])
        capable = [a for a in d["compose_assets"] if a["in_prompt"]]
        print("\n=== definitions that can reach the composer ===")
        for a in capable:
            print(f"{a['name']:<24} {a['note']}")
        blind = [a for a in d["compose_assets"] if not a["in_prompt"]]
        if blind:
            print("\n=== NOT reaching the composer ===")
            for a in blind:
                print(f"{a['name']:<24} {a['note']}")
    return 0


def _lens_log(args: argparse.Namespace) -> int:
    """`dst lens log <name>` — the published history as a change log.

    The versions and the diff API existed; reading history still meant
    dashboard archaeology. One table, newest first: version, date, who
    published (0051 actors), and the summary the publish recorded. The
    reviewer question this answers is "what changed on this lens and who did
    it" — in the terminal, where the apply that caused it just ran."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/lenses/{args.name}/versions", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    rows = r.raise_for_status().json()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"lens '{args.name}' has no published versions yet")
        return 0
    print(
        _table(
            ["version", "published", "by", "summary"],
            [
                [
                    f"v{v['version']}",
                    str(v["created_at"])[:10],
                    _actor_word(str(v.get("created_by") or "")),
                    v["summary"] or "—",
                ]
                for v in rows
            ],
        )
    )
    return 0


def _lens_list(args: argparse.Namespace) -> int:
    """`dst lens list` — the deployed lenses, from the SERVER.

    Enumerating lenses used to mean reading plan output or listing lenses/*/ on
    disk — but files and server state can diverge by design (file absence never
    deletes; API/cloud-born lenses are server-only until adopted), and the
    documented drift case had no CLI read path. The `not in files` marker is
    the adoption cue: `dst export --lens <name>` brings it under the files."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/lenses", headers=headers, timeout=60)
    lenses = r.raise_for_status().json()
    if args.json:
        print(json.dumps(lenses, indent=2, ensure_ascii=False))
        return 0
    if not lenses:
        print("no lenses on this server — `dst apply` deploys the project's")
        return 0
    print(
        _table(
            ["lens", "status", "shape", "queries", "last queried", "origin"],
            [
                [
                    lens["name"],
                    lens["status"],
                    f"{lens['entity_count']}e/{lens['definition_count']}d",
                    lens["query_count"],
                    str(lens.get("last_queried_at") or "—")[:10],
                    "files"
                    if lens.get("from_files", True)
                    else f"not in files — adopt with `dst export --lens {lens['name']}`",
                ]
                for lens in lenses
            ],
        )
    )
    return 0


def _actor_word(actor: str) -> str:
    """`human:ana@corp` → ana@corp; `token:ci` → token 'ci'; '' → —."""
    if not actor:
        return "—"
    if actor.startswith("human:"):
        return actor.removeprefix("human:")
    if actor.startswith("token:"):
        return f"token '{actor.removeprefix('token:')}'"
    return actor


def _lens(args: argparse.Namespace) -> int:
    """`dst lens rm <name>` — the explicit deletion verb the doctrine
    requires (file absence never deletes; plan only flags server-only lenses).
    Prints the cascade BEFORE acting and asks y/N (`--yes` headless) — the
    semantic-rm guard style, with the blast radius spelled out.
    `dst lens prompt <name> "<question>"` prints what the model would see.
    `dst lens log <name>` prints the published-version history."""
    import httpx

    if args.action == "list":
        return _lens_list(args)
    if not args.name:
        print(f"error: `dst lens {args.action}` needs a lens name", file=sys.stderr)
        return 2
    if args.action == "prompt":
        return _prompt_preview(args)
    if args.action == "log":
        return _lens_log(args)
    url, headers = _client(args)
    counts: dict[str, int] = {}
    for label, path in (
        ("lens versions", f"/mgmt/lenses/{args.name}/versions"),
        ("certified answers", f"/mgmt/lenses/{args.name}/certified"),
        ("eval cases", f"/mgmt/lenses/{args.name}/evals/cases"),
    ):
        r = httpx.get(f"{url}{path}", headers=headers, timeout=60)
        if r.status_code == 404:  # the versions endpoint checks the lens exists
            print(f"no lens '{args.name}'", file=sys.stderr)
            return 1
        counts[label] = len(r.raise_for_status().json())
    cascade = ", ".join(f"{label}: {n}" for label, n in counts.items())
    print(
        f"deleting lens '{args.name}' also deletes — {cascade} "
        "(plus its eval runs, context chunks, and patch candidates; "
        "request log and review rulings are kept)"
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing without confirmation — pass --yes", file=sys.stderr)
            return 1
        if input("delete? y/N: ").strip().lower() != "y":
            print("aborted — nothing deleted")
            return 1
    r = httpx.delete(f"{url}/mgmt/lenses/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(f"removed lens '{args.name}'")
    return 0


def _connection(args: argparse.Namespace) -> int:
    """`dst connection rm <name>` — the CLI face of DELETE /mgmt/connections.
    Same doctrine as `lens rm`/`semantic rm` (file absence never deletes;
    plan flags server-only connections forever otherwise), same guard style:
    dependents shown BEFORE acting, y/N unless --yes. Refuses while lenses
    still read through the connection — re-point or remove them first."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/connections/{args.name}/dependents", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no connection '{args.name}'", file=sys.stderr)
        return 1
    lenses = r.raise_for_status().json().get("lenses", [])
    if lenses:
        names = ", ".join(str(ln.get("name", "?")) for ln in lenses)
        print(
            f"refusing: {len(lenses)} lens(es) still read through '{args.name}': {names} — "
            "re-point or `dst lens rm` them first",
            file=sys.stderr,
        )
        return 1
    print(f"deleting connection '{args.name}' (stored credentials go with it)")
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing without confirmation — pass --yes", file=sys.stderr)
            return 1
        if input("delete? y/N: ").strip().lower() != "y":
            print("aborted — nothing deleted")
            return 1
    r = httpx.delete(f"{url}/mgmt/connections/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no connection '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(
        f"removed connection '{args.name}' (if dst.yaml still declares it, "
        "the next apply re-creates it)"
    )
    return 0


def _keys(args: argparse.Namespace) -> int:
    import httpx

    url, headers = _client(args)
    if args.action == "create":
        if not args.caller:
            print("error: --caller is required for create", file=sys.stderr)
            return 1
        r = httpx.post(
            f"{url}/mgmt/callers", headers=headers, json={"name": args.caller}, timeout=60
        )
        if r.status_code not in (201, 409):
            r.raise_for_status()
        r = httpx.post(f"{url}/mgmt/callers/{args.caller}/keys", headers=headers, timeout=60)
        key = r.raise_for_status().json()
        print(f"caller: {args.caller}")
        print(f"key (store it now — shown once): {key['key']}")
        return 0
    r = httpx.get(f"{url}/mgmt/callers", headers=headers, timeout=60)
    for c in r.raise_for_status().json():
        print(c.get("name"), "·", c.get("description") or "")
    return 0


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


def _revoke_key(args: argparse.Namespace) -> int:
    """`dst revoke-key --caller NAME` — revoke that caller's active keys, in
    ONE org.

    It used to run `UPDATE api_key … WHERE caller.name = :n` on the admin engine
    with no org predicate, so revoking `alex` from the `demoland` project also
    revoked `alex` in `me`: `revoked 2 active key(s)`, measured. Every other
    caller verb goes through /mgmt/callers, which is org-scoped by the admin
    token; this one bypassed the server and wrote raw SQL, so it inherited no
    scope at all. Revocation is an access-control operation — reaching into a
    tenant the operator never named is the one thing it must not do.

    Scoped by ``_project_org``, and the org is NAMED in the output so the
    operator can see what they just did. Unscopable is REFUSED, never widened:
    the read-only sweep `dst test` can afford to say "every org" out loud
    and carry on, and this cannot."""
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.db.session import admin_engine

    scoped = _project_org(args)
    if scoped is None:
        print(
            "error: no org to revoke in — pass --org NAME, or run from a project whose "
            ".env carries DST_ADMIN_TOKEN (`dst bootstrap` writes it). Revoking "
            "across every org in the database is not a default",
            file=sys.stderr,
        )
        return 1
    org_id, org_name = scoped
    with admin_engine.begin() as c:
        # caller is FORCE-RLS: without the org GUC a managed-Postgres admin role
        # (no BYPASSRLS) matches zero callers and the revoke silently no-ops.
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
        n = c.execute(
            text(
                "UPDATE api_key SET revoked_at = now() FROM caller "
                "WHERE api_key.caller_id = caller.id AND caller.name = :n "
                "AND api_key.org_id = :o AND api_key.revoked_at IS NULL"
            ),
            {"n": args.caller, "o": org_id},
        ).rowcount
    print(f"revoked {n} active key(s) for caller '{args.caller}' in org '{org_name}'")
    return 0 if n else 1


def _revoke_token(args: argparse.Namespace) -> int:
    """`dst revoke-token <raw>` — kill one credential, given the credential itself.

    For the case a `dstadm_` token reaches somewhere it should not — a tracked
    file, a log, a chat message. `revoke-key` cannot help: it takes a caller name
    and only touches `api_key`, and admin tokens have no caller.
    The documented remedy was "revoke by hash" — i.e. a hand-written UPDATE against
    the admin engine, at the exact moment an operator is rattled and least likely to
    get a WHERE clause right.

    Takes the RAW token because that is what a leak hands you: the string in the
    file, the CI log, the screenshot. Hashing it here means the caller never has to
    know how we store credentials, and never has to match one by substring.

    Deliberately org-agnostic, unlike `revoke-key`. That verb refuses to act without
    a named org because a caller NAME is ambiguous across tenants and over-revoking
    is its failure mode. A token hash is unique by construction (`UNIQUE` on both
    columns), so there is exactly one row it can touch in exactly one org — naming
    the org would add a way to fail, not a guard. The org is printed so the operator
    sees which tenant they just touched.
    """
    _adopt_project_env(args)
    from sqlalchemy import text

    from services.auth.tokens import hash_token
    from services.db.session import admin_engine

    token_hash = hash_token(args.token.strip())
    # Both stores: the operator should not have to know which one a prefix implies,
    # and `dsto_` OAuth tokens live in api_key beside `dst_` caller keys.
    targets = (("admin_token", "token_hash"), ("api_key", "key_hash"))
    with admin_engine.begin() as c:
        for table, column in targets:
            row = c.execute(
                text(
                    f"UPDATE {table} SET revoked_at = now() "  # noqa: S608 — literals, not input
                    f"WHERE {column} = :h AND revoked_at IS NULL RETURNING org_id"
                ),
                {"h": token_hash},
            ).first()
            if row is not None:
                org = c.execute(text("SELECT name FROM org WHERE id = :o"), {"o": row[0]}).scalar()
                print(f"revoked 1 {table} in org '{org}'")
                return 0
        # "Already dead" and "never existed here" mean opposite things to an operator
        # racing to contain a leak — one says stop, the other says you are looking in
        # the wrong database. Never collapse them into a single success.
        for table, column in targets:
            if c.execute(
                text(f"SELECT 1 FROM {table} WHERE {column} = :h"),  # noqa: S608 — literals
                {"h": token_hash},
            ).first():
                print(f"already revoked ({table})")
                return 0
    print(
        "no such credential in this database — check DATABASE_URL points at the "
        "deployment this token belongs to",
        file=sys.stderr,
    )
    return 1


def _test_scope(args: argparse.Namespace) -> list[tuple[uuid.UUID, str]] | None:
    """Which orgs `dst test` sweeps — THIS project's, not every tenant in the
    database. None (after printing why) when the scope resolves to nothing.

    It used to iterate every org and filter on lens NAME alone, so one
    `dst test tox` swept four different orgs' identically-named `tox`
    lenses — four copies of the same corpus, 37 minutes, and nothing in the
    output naming whose `PASS` was whose. A green suite that silently tested
    someone else's lens is worse than a red one: it is the same lie
    `--dir`/`_env_dirs` exist to prevent, entering through the org loop instead
    of through .env.

    Scope resolution is ``_project_org``; what is left here is this verb's own
    answer to an unscopable run — every org, said out loud, which a read-only
    sweep can afford. A named org that does not exist is an error, never a
    silent full sweep."""
    from sqlalchemy import text

    from services.config import resolve_env_ref
    from services.db.session import admin_engine

    scoped = _project_org(args)
    if scoped is not None:
        return [scoped]
    if args.org:
        print(f"error: no org named '{args.org}'", file=sys.stderr)
        return None
    if resolve_env_ref("DST_ADMIN_TOKEN", dirs=_env_dirs(args)):
        print(
            f"note: the admin token in {_env_dirs(args)[0]}/.env matches no org in this "
            "database — sweeping every org (use --org to scope)",
            file=sys.stderr,
        )
    else:
        print(
            "note: no DST_ADMIN_TOKEN for this project — sweeping EVERY org in the "
            "database; lens names are not unique across orgs, so use --org to scope",
            file=sys.stderr,
        )
    with admin_engine.connect() as c:
        every = c.execute(text("SELECT id, name FROM org ORDER BY created_at"))
        return [(r[0], r[1]) for r in every]


def _ledger_line(plain: str, *, ok: bool, elapsed: float | None) -> str:
    """One test-ledger row: the existing `VERDICT  label: question` bytes with
    the verdict painted and a dim latency column appended. The
    plain text is the parse contract — padding is computed on it, not on the
    escape-laden string."""
    verdict, _, rest = plain.partition("  ")
    painted = (style.good if ok else style.bad)(verdict) + "  " + rest
    if elapsed is None:
        return painted
    pad = " " * max(2, 78 - len(plain))
    return painted + pad + style.dim(f"{elapsed:.1f}s")


def _test(args: argparse.Namespace) -> int:
    """`dst test [lens]` — the certified corpus as the regression suite:
    for each ACTIVE certified answer, execute its stored SQL (the
    oracle) AND run its question through the generation pipeline with certified
    matching disabled, then compare executed results. Approved behavioral
    expectations (expect: clarify|refuse) run alongside, scored on response
    shape. Always the FULL active corpus (apply's gate scopes to
    binding-affected answers; this verb is the cron/CI full sweep). In-process
    against the configured database, like migrate/bootstrap — no server needed;
    ``--dir`` names the project whose .env supplies DATABASE_URL and the
    provider keys, so the one gate-verification command is invocable from
    outside its own directory. ORG-SCOPED (see ``_test_scope``), and every
    result line names its org. A green answer is a re-verification: its
    bindings re-stamp to current hashes (the plan's re-verify flag clears
    through evidence). Exit 1 on any divergence or failed expectation."""
    _adopt_project_env(args)
    if getattr(args, "url", None) or getattr(args, "token", None):
        # Accepted for trio-uniformity (the `bootstrap --url` precedent), but
        # never silently: a token naming prod while DATABASE_URL names localhost
        # is exactly the confusion this verb must not create.
        print(
            "note: --url/--token are ignored — `dst test` runs in-process against "
            "DATABASE_URL, not through a server; --dir picks the project whose .env "
            "supplies it",
            file=sys.stderr,
        )
    from services.certify import store as certify_store
    from services.certify.bindings import restamp_bindings
    from services.contracts.eval import EvalResult
    from services.db.session import org_session
    from services.evals import store as eval_store
    from services.evals.certified_suite import format_result, run_certified_suite
    from services.evals.runner import run_behavioral
    from services.evals.service import _row_to_case
    from services.lenses import store as lens_store
    from services.lenses.connections import resolve_connector
    from services.llm import registry
    from services.runtime import assembly
    from services.runtime.answer import AnswerComposer

    if args.all:
        args.lens = None
    if registry.resolve(registry.tier("smart")) is None:
        print(
            "error: no smart-tier model resolves — set smart_model (and its API key) on a provider",
            file=sys.stderr,
        )
        return 1
    orgs = _test_scope(args)
    if orgs is None:
        return 1
    total = failed = 0
    cert_n = beh_n = candidates_n = 0
    found_lens = False
    emit_json = bool(getattr(args, "as_json", False))
    rows: list[dict[str, object]] = []

    def say(text: str) -> None:  # prose is stdout; --json swallows it for the object below
        if not emit_json:
            print(text)

    for oid, org_name in orgs:
        with org_session(oid) as session:
            for name, _dn, _desc, bundle in lens_store.list_published(session):
                if args.lens and name != args.lens:
                    continue
                found_lens = True
                # Every result line names the ORG — see _test_scope. A bare
                # "PASS  tox: …" from a sweep that hit four identically-named
                # lenses says nothing about whose corpus just went green.
                label = f"{org_name}/{name}"
                answers = [
                    a
                    for a in certify_store.list_for_lens(session, name)
                    if certify_store.is_active(a.status)
                ]
                # The sweep TESTS unembedded answers (their oracle is fine)
                # but must never imply they serve — matching is pgvector cosine,
                # and a NULL vector can never be returned. Said before the
                # results so it survives a long run's scrollback.
                if unembedded := certify_store.count_unembedded(session, name):
                    say(f"{label}: {certify_store.UNEMBEDDED_WARNING.format(n=unembedded)}")
                behavioral = [
                    case
                    for case in (
                        _row_to_case(row)
                        for row in eval_store.list_cases(session, name, status="approved")
                    )
                    if case.expect is not None
                ]
                # Cases that exist but will NOT run this sweep. Without this line an
                # unarmed suite and a fully-passing one print the same shape:
                # `passed 11/11` while candidates silently sit out.
                lens_candidates = len(
                    [c for c in eval_store.list_cases(session, name) if c.status == "candidate"]
                )
                candidates_n += lens_candidates
                if tag_filter := getattr(args, "tag", None) or []:
                    # --tag runs a case SLICE (persona:cfo, intent:discriminator).
                    # Certified answers carry no tags, so a tagged run is
                    # behavioral-only by construction.
                    behavioral = [c for c in behavioral if any(t in c.tags for t in tag_filter)]
                    answers = []
                if not answers and not behavioral:
                    slice_note = f" matching --tag {', '.join(tag_filter)}" if tag_filter else ""
                    parked = (
                        f" ({lens_candidates} candidate case(s) not run — promote with "
                        "status: approved)"
                        if lens_candidates
                        else ""
                    )
                    say(
                        f"{label}: no active certified answers, no behavioral "
                        f"cases{slice_note}{parked}"
                    )
                    continue
                # The lens's OWN model, with NO tier fallback. Falling back
                # dialled a different vendor for a lens pinned to one this
                # install does not have — the sweep would then report PASS for
                # a model the lens never serves on. Untestable is said out loud
                # and counted, never quietly tested against someone else.
                lens_ref = bundle.config.model.model_ref()
                resolved = registry.resolve(lens_ref)
                if resolved is None:
                    total += 1
                    failed += 1
                    reason = registry.unservable_detail(lens_ref)
                    say(
                        style.warn("SKIP")
                        + f"  {label}: NOT verified — this lens's model cannot be served "
                        f"here: {reason}"
                    )
                    rows.append(
                        {"org": org_name, "lens": name, "verdict": "skip", "reason": reason}
                    )
                    continue
                model_name = resolved.name
                connector = resolve_connector(bundle.config.connections[0], oid)
                composer = AnswerComposer(resolved.llm, model=model_name)
                # Liveness before the first slow call: in silence, working, hung and
                # rate-limited all look identical to the reader.
                progress_bits = []
                if answers:
                    progress_bits.append(f"{len(answers)} certified")
                if behavioral:
                    progress_bits.append(f"{len(behavioral)} behavioral")
                if lens_candidates:
                    progress_bits.append(f"{lens_candidates} candidate(s) not run")
                say(style.dim(f"{label}: running {' + '.join(progress_bits)} …"))
                if answers:
                    assemble_for, generators_for = assembly.eval_harness(bundle, oid, resolved)
                    outcome = run_certified_suite(
                        connector=connector,
                        lens=name,
                        answers=answers,
                        assemble_for=assemble_for,
                        generators_for=generators_for,
                        composer=composer,
                        model_name=model_name,
                    )
                    by_id = {r.answer_id: r for r in outcome.results}
                    for a in answers:
                        r = by_id[a.id]
                        total += 1
                        cert_n += 1
                        rows.append(
                            {
                                "org": org_name,
                                "lens": name,
                                "question": r.question,
                                "verdict": "pass" if r.passed else "fail",
                                "certified": format_result(r.oracle_result),
                                "generated": format_result(r.generated_result),
                                "reason": r.reason,
                                "elapsed_s": r.elapsed_s,
                            }
                        )
                        if not r.passed:
                            failed += 1
                            why = f" ({r.reason})" if r.reason else ""
                            say(
                                _ledger_line(
                                    f"FAIL  {label}: {r.question} — certified SQL → "
                                    f"{format_result(r.oracle_result)}, generated → "
                                    f"{format_result(r.generated_result)}{why}",
                                    ok=False,
                                    elapsed=r.elapsed_s,
                                )
                            )
                            continue
                        say(
                            _ledger_line(
                                f"PASS  {label}: {r.question}", ok=True, elapsed=r.elapsed_s
                            )
                        )
                        # Evidence-based re-verify: a passing
                        # test re-stamps the answer's bindings to current hashes.
                        # Templates re-stamp from the RENDERED sample-bound SQL —
                        # raw {placeholder} SQL never parses.
                        from services.evals.certified_suite import _render_template_case

                        rendered = (
                            _render_template_case(a, bundle.semantic_model.dialect)
                            if a.slots
                            else None
                        )
                        stamp_sql = rendered[0] if rendered else a.sql
                        fresh = restamp_bindings(stamp_sql, a.bindings, bundle.semantic_model)
                        if fresh and fresh != (a.bindings or {}):
                            certify_store.update(
                                session,
                                a.id,
                                sql=a.sql,
                                verified_value=a.verified_value,
                                bindings=fresh,
                            )
                if behavioral:
                    # The slim rest of evals/cases.yaml: expect: clarify|refuse
                    # pins run alongside the corpus, as the scaffold promises.
                    assemble_q, b_generators_for = assembly.question_harness(bundle, oid, resolved)
                    b_outcome = run_behavioral(
                        connector=connector,
                        lens=name,
                        cases=behavioral,
                        assemble_for=assemble_q,
                        generators_for=b_generators_for,
                        composer=composer,
                        model_name=model_name,
                    )
                    by_case = {res.case_id: res for res in b_outcome.results}
                    for case in behavioral:
                        res = by_case.get(case.id)
                        if res is None:
                            continue
                        total += 1
                        beh_n += 1
                        rows.append(
                            {
                                "org": org_name,
                                "lens": name,
                                "question": case.question,
                                "expect": case.expect,
                                "verdict": "pass" if res.passed else "fail",
                                "reason": res.reason,
                                **({"tags": case.tags} if case.tags else {}),
                            }
                        )
                        if res.passed:
                            say(
                                _ledger_line(
                                    f"PASS  {label}: {case.question} [expect: {case.expect}]",
                                    ok=True,
                                    elapsed=None,
                                )
                            )
                        else:
                            failed += 1
                            why = f" ({res.reason})" if res.reason else ""
                            say(
                                _ledger_line(
                                    f"FAIL  {label}: {case.question} [expect: {case.expect}]{why}",
                                    ok=False,
                                    elapsed=None,
                                )
                            )
                # Every result is recorded: the run lands in eval_run /
                # eval_result (mode "test"), the same tables the publish gate
                # writes, so accuracy is a queryable trend whoever runs the
                # sweep — CI, cron, or a laptop. Output bytes stay unchanged.
                recorded: list[EvalResult] = []
                if answers:
                    for cr in outcome.results:
                        recorded.append(
                            EvalResult(
                                run_id="",
                                case_id=f"certified:{cr.answer_id}",
                                question=cr.question,
                                passed=cr.passed,
                                grade="pass"
                                if cr.passed
                                else ("errored" if "error" in cr.oracle_result else "fail"),
                                checks=(
                                    {"wrong_at": "rows"}
                                    if not cr.passed and "error" not in cr.oracle_result
                                    else {}
                                ),
                                reason=cr.reason,
                                actual_sql=cr.generated_sql,
                            )
                        )
                if behavioral:
                    recorded.extend(b_outcome.results)
                if recorded:
                    r_passed = sum(1 for r in recorded if r.passed)
                    r_errored = sum(1 for r in recorded if r.grade == "errored")
                    run_id = eval_store.create_run(
                        session,
                        name,
                        "test",
                        score=r_passed / len(recorded),
                        passed=r_passed,
                        failed=len(recorded) - r_passed - r_errored,
                        errored=r_errored,
                    )
                    for rec in recorded:
                        rec.run_id = run_id
                    eval_store.record_results(session, run_id, recorded)
    scope = ", ".join(n for _, n in orgs)
    if args.lens and not found_lens:
        print(f"error: no published lens '{args.lens}' in org {scope}", file=sys.stderr)
        return 1
    if emit_json:
        print(
            json.dumps(
                {
                    "results": rows,
                    "passed": total - failed,
                    "total": total,
                    "certified": cert_n,
                    "behavioral": beh_n,
                    "candidates_not_run": candidates_n,
                    "orgs": scope,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if rows:
            print(style.accent("─" * 64))
        # The denominator says what it is made of: a bare `passed 11/11`
        # reads as a green suite when it may be a handful of certified plus
        # behavioral cases with candidates not run — an unarmed suite and a passing one
        # must not print the same shape).
        composition = f"{cert_n} certified + {beh_n} behavioral"
        print(style.bold(f"{total - failed}/{total} passed") + f" ({composition}) in org {scope}")
        if candidates_n:
            print(
                style.warn(
                    f"note: {candidates_n} candidate case(s) were NOT run — promote with "
                    "status: approved in evals/cases.yaml (or the approve endpoint) to "
                    "include them"
                )
            )
    if failed:
        return 1
    if total == 0:
        # `0/0 passed` with exit 0 is indistinguishable from `10/10 passed` to a
        # script, to CI, or to anyone skimming output — a suite that verifies
        # nothing would report as assurance, and a definition change could break
        # a metric across an apply with the gate reporting green.
        #
        # Exit 4 is the honest third outcome, in the same spirit as `query`'s
        # 0 answered / 3 declined / 1 broke: nothing passed and nothing failed
        # because there was nothing to verify. Scripts branch on it; a CI gate
        # treats it as not-green without having to parse prose.
        print(
            "warning: nothing was verified — this lens has no certified answers and no "
            "behavioral cases, so this run could not have failed. Add certified answers "
            "(`dst reviews rule <id> --verdict approve --certify`) or eval cases.",
            file=sys.stderr,
        )
        return 4
    return 0


def _observe(args: argparse.Namespace) -> int:
    """Who has been using this layer, and what for — from the terminal.

    The same view the API and the dashboard expose, on the CLI: "who has been
    using this and what for" is a question data teams answer from a terminal, and
    governance reachable only from a browser is not reachable by the callers most
    likely to need it.

    Read-only, admin-authed (`/mgmt/observe/*`), four shapes:

      dst observe                 headline + per-caller usage — the CFO answer
      dst observe callers         who, how many, how much, declines and errors
      dst observe requests        what they actually asked
      dst observe show <req_id>   one request: question, SQL, confidence

    `--json` on any of them for the parseable form; agents should prefer it.
    """
    import httpx

    url, headers = _client(args)

    def get(path: str, **params: str | int | None) -> object:
        r = httpx.get(
            f"{url}/mgmt/observe{path}",
            headers=headers,
            params={k: v for k, v in params.items() if v is not None},
            timeout=args.timeout,
        )
        if r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            raise SystemExit(1)
        return r.json()

    action = getattr(args, "action", None) or "summary"

    if action == "show":
        trace = get(f"/requests/{args.request_id}")
        if args.json:
            print(json.dumps(trace, indent=2))
            return 0
        assert isinstance(trace, dict)
        for key in ("request_id", "created_at", "caller", "lens", "status", "confidence"):
            if trace.get(key) is not None:
                print(f"{key + ':':<14}{trace[key]}")
        if q := trace.get("question"):
            print(f"\n{q}")
        if sql := trace.get("sql"):
            print(f"\n{sql}")
        return 0

    if action == "requests":
        rows = get("/requests", lens=args.lens, status=args.status, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        assert isinstance(rows, list)
        if not rows:
            print("no requests recorded")
            return 0
        for r_ in rows:
            when = str(r_.get("created_at") or "")[:19].replace("T", " ")
            # Two-space gutters BETWEEN columns, not width-only padding: a
            # 14-char lens name or the 13-char 'clarification' status fills its
            # field exactly, and pad-only columns then run together.
            print(
                f"{when}  {str(r_.get('caller') or '-'):<16}  "
                f"{str(r_.get('lens') or '-'):<14}  "
                f"{str(r_.get('status') or '-'):<13}  "
                f"{str(r_.get('question') or '')[:60]}"
            )
        return 0

    callers = get("/callers", lens=args.lens)
    assert isinstance(callers, list)
    if action == "callers" and args.json:
        print(json.dumps(callers, indent=2))
        return 0

    kpis = get("/kpis") if action == "summary" else None
    if args.json:
        print(json.dumps({"kpis": kpis, "callers": callers}, indent=2))
        return 0

    if isinstance(kpis, dict):
        head = f"{kpis.get('queries', 0)} queries · ${float(kpis.get('ai_cost_usd') or 0):.4f} AI"
        outcomes = kpis.get("outcomes")
        if isinstance(outcomes, dict):
            # Declines are governed outcomes (authoring/scope work), errors are
            # faults — printing them as one number turns a handful of real faults
            # plus a pile of governed declines into one alarming "error rate", and
            # the response to that number is always wrong. Only nonzero buckets,
            # ok first, error last.
            order = ("ok", "refused", "clarification", "rejected", "error")
            split = " · ".join(f"{outcomes.get(k, 0)} {k}" for k in order if outcomes.get(k))
            print(f"{head} · {split}" if split else head)
        else:  # pre-split server: 'errors' there means every non-ok outcome
            print(f"{head} · {kpis.get('errors', 0)} non-ok")
        print()
    if not callers:
        # Distinguish "nobody has used it" from "the surface is broken" — the same
        # confusion that made an empty router page read as a bug.
        print("no caller activity recorded yet")
        return 0
    print(f"{'caller':<20}{'queries':>9}{'cost':>11}{'declined':>10}{'errors':>8}")
    for c in callers:
        declined = c.get("declined")
        print(
            f"{str(c.get('caller') or '-'):<20}{c.get('queries', 0):>9}"
            f"{float(c.get('cost_usd') or 0):>11.4f}"
            f"{('-' if declined is None else declined):>10}{c.get('errors', 0):>8}"
        )
    return 0


def _reviews(args: argparse.Namespace) -> int:
    """The review queue — the data team's whole queue under an admin token, or
    just YOUR OWN tickets under a caller key (`--key dst_…`, or a project whose
    .env holds only DST_API_KEY).

    The caller half exists because filing a correction is only half a loop. A
    business user who reports a wrong answer learns nothing more unless someone
    tells him out of band; `/mgmt/reviews` is admin-only and correctly so. The
    data plane's `/v1/reviews` answers the one question he has — what happened
    to the thing I reported — and answers it about his requests only."""
    import httpx

    url, headers = _client(args, caller_key_ok=True, admin_first=True)
    if args.watch:
        if args.as_caller:
            print(
                "error: --watch polls the data team's queue and needs an admin token; "
                "`dst reviews --key …` lists your own tickets",
                file=sys.stderr,
            )
            return 1
        return _reviews_watch(args, url, headers)
    # Unfiltered by default — --json included. Defaulting --json to needs_human
    # hid auto-approved origin:ai tickets and made auto_review look like a no-op.
    # Agents filter themselves with --state/--origin.
    params = {"state": args.state} if args.state else {}
    path = "/v1/reviews" if args.as_caller else "/mgmt/reviews"
    r = httpx.get(f"{url}{path}", headers=headers, params=params, timeout=60)
    tickets = r.raise_for_status().json()
    if args.origin:
        # Client-side: the list endpoint filters by state only.
        tickets = [t for t in tickets if t.get("origin") == args.origin]
    if args.json:
        print(json.dumps(tickets))
        return 0
    if not tickets:
        scope = "you have filed no corrections" if args.as_caller else "review queue is empty"
        print(scope + (f" (state={args.state})" if args.state else ""))
        return 0
    for t in tickets:
        verdict = t.get("human_verdict") or t.get("ai_verdict") or "-"
        print(
            f"{t['ticket_id']}  {t['state']:<10} {t['lens']:<20} "
            f"caller={t['caller']:<12} origin={t.get('origin') or 'human':<6} verdict={verdict}"
        )
    return 0


def _reviews_watch(args: argparse.Namespace, url: str, headers: dict[str, str]) -> int:
    """Poll the needs_human queue; print each ticket once as it first appears
    (id, lens, question). In-memory seen-set only — Ctrl-C stops it; no
    daemon, no state file (agent- and cron-friendly).

    Takes the resolved client from `_reviews`: the door has to be chosen before
    the branch, so a caller key gets the refusal that names its own read path
    instead of a bare "no admin token"."""
    import time

    import httpx

    seen: set[str] = set()
    try:
        while True:
            try:
                r = httpx.get(
                    f"{url}/mgmt/reviews",
                    headers=headers,
                    params={"state": "needs_human"},
                    timeout=60,
                )
                tickets = r.raise_for_status().json()
            except httpx.HTTPError as exc:
                # A watcher must ride out transient 5xx/timeouts, not die on them.
                print(f"watch: {exc} — retrying", file=sys.stderr, flush=True)
                time.sleep(args.interval)
                continue
            for t in tickets:
                if t["ticket_id"] in seen:
                    continue
                seen.add(t["ticket_id"])
                # The ticket doesn't carry the question — join the request log,
                # exactly as the UI's queue does.
                rq = httpx.get(
                    f"{url}/mgmt/observe/requests/{t['request_id']}", headers=headers, timeout=60
                )
                q = rq.json().get("question") if rq.status_code == 200 else None
                line = " ".join(q.split()) if q else t["request_id"]
                print(f"{t['ticket_id']}  {t['lens']}  {line}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _comment_head(text: str) -> list[str]:
    """The file's leading comment block (shape-teaching headers) — kept verbatim
    across a rewrite; a migration must not eat the docs that live in the file."""
    head: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    while head and not head[-1].strip():
        head.pop()
    return head


def _migrate_context(root: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(leaf→source table, connection name→dialect, ambiguous leaves) for a project.

    Eval-plane SQL names LEAF tables by design; certified SQL executes against
    the LIVE warehouse and needs the fully-qualified names. A
    leaf claimed by two entities with different sources is unmappable — named,
    never guessed."""
    from services.project.compile import CompileError, dialect_for
    from services.project.loader import split_semantic
    from services.project.schema import parse_project_yaml
    from services.project.warehouse_drift import parse_layer

    entities_by_path, _defs = parse_layer(split_semantic(_read_project(root)))
    leaf_to_table: dict[str, str] = {}
    ambiguous: set[str] = set()
    for entity in entities_by_path.values():
        leaf = entity.source.table.split(".")[-1].lower()
        if leaf in leaf_to_table and leaf_to_table[leaf] != entity.source.table:
            ambiguous.add(leaf)
        leaf_to_table[leaf] = entity.source.table
    for leaf in ambiguous:
        del leaf_to_table[leaf]
    dialects: dict[str, str] = {}
    project_path = root / "dst.yaml"
    if project_path.exists():
        try:
            project = parse_project_yaml(project_path.read_text(encoding="utf-8"))
        except ValueError:
            project = None
        if project is not None:
            for name, decl in project.connections.items():
                try:
                    dialects[name] = dialect_for(decl.type)
                except CompileError:
                    continue  # a context source — no dialect
    return leaf_to_table, dialects, sorted(ambiguous)


def _lens_dialect_local(lens_dir: Path, dialects: dict[str, str]) -> str | None:
    """The lens's SQL dialect from its lens.yaml connections — file-first."""
    import yaml

    lens_yaml = lens_dir / "lens.yaml"
    if lens_yaml.exists():
        try:
            data = yaml.safe_load(lens_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        for conn in data.get("connections") or []:
            if str(conn) in dialects:
                return dialects[str(conn)]
    if len(dialects) == 1:  # one warehouse in the project — no ambiguity
        return next(iter(dialects.values()))
    return None


def _evals_gate(args: argparse.Namespace) -> int:
    """`dst evals gate <lens>` — run ONE lens's publish gate (the exact code
    path apply gates on) and print the decision, publishing nothing. Without it,
    the only way to learn one lens's verdict is a full apply across every armed
    gate in the project, and the score it computes is invisible outside the
    database.
    Server-side the staged eval run rolls back, so a red dry run never becomes
    the baseline the next real apply is judged against."""
    import httpx

    if not args.lens:
        print("error: `dst evals gate` needs a lens name", file=sys.stderr)
        return 2
    url, headers = _client(args)
    # The CANDIDATE tree rides along: gating the stored bundle
    # false-greened every unpublished shared-asset change — the exact pushes
    # a dry run exists for. Outside a project dir the server gates the stored
    # bundle and says so.
    files = _read_project(Path(getattr(args, "dir", ".") or "."))
    payload = {"files": files} if files else None
    if not files:
        print(
            "note: no project files under --dir — gating the SERVER's stored bundle; "
            "unpublished file changes are not reflected",
            file=sys.stderr,
        )
    r = httpx.post(
        f"{url}/mgmt/lenses/{args.lens}/evals/gate",
        headers=headers,
        json=payload,
        timeout=900.0,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = r.text[:300]
        print(f"error: gate check failed (HTTP {r.status_code}): {detail}", file=sys.stderr)
        return 1
    out = r.json()
    if getattr(args, "as_json", False):
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        label = str(out.get("gate"))
        d: dict[str, Any] = out.get("gate_detail") or {}
        if label == "passed":
            painted = style.good(label)
        elif label == "off" or label.startswith("skipped"):
            painted = style.warn(label)
        else:
            painted = style.bad(label)
        print(f"{style.accent('lens ' + args.lens)}: gate {painted}")
        if d.get("score") is not None:
            prev = d.get("prev_score")
            print(f"  score {d['score']}" + (f" (prev {prev})" if prev is not None else ""))
        for q in d.get("failing") or []:
            print(f"  {style.bad('failing:')} {q}")
        for m in d.get("certified_failures") or []:
            print(f"  {style.bad('certified:')} {m}")
        for cid in d.get("flaky") or []:
            print(
                f"  {style.warn('flaky:')} {cid} " + style.dim("(passed on retry — certify to pin)")
            )
        if d and not d.get("gated"):
            print(f"  {style.warn('not scored:')} {d.get('detail') or d.get('skip_reason')}")
        for err in out.get("errors") or []:
            print(f"  {style.bad('error:')} {err}")
        if note := out.get("scope_note"):
            print(f"  {style.warn('scope:')} {note}")
        print(style.dim("  dry run — nothing published, no baseline recorded"))
    red = label not in ("passed", "off") and not label.startswith("skipped")
    return 1 if red else 0


def _evals_dispatch(args: argparse.Namespace) -> int:
    if args.action == "gate":
        return _evals_gate(args)
    return _evals_migrate(args)


def _evals_migrate(args: argparse.Namespace) -> int:
    """`dst evals migrate` — LOCAL, file-to-file, no server: every APPROVED
    value-shaped eval case (has expected_sql) becomes a
    certified_answers.yaml entry (the certified corpus IS the regression
    suite); cases.yaml keeps behavioral entries and anything not yet
    approved — migration is a promotion, and only a reviewed case may enter
    the serving corpus. Leaf table names are repointed at their live sources
    (the eval plane's own mapping rule) so migrated SQL actually executes.
    Nothing lands until the user reviews and runs `dst apply` — this edits
    files, exactly like they would by hand."""
    import yaml

    from services.evals.rewrite import rewrite_to_sources

    root = Path(args.dir)
    case_files = sorted((root / "lenses").glob("*/evals/cases.yaml"))
    if not case_files:
        print(f"no lenses with evals/cases.yaml under {root}/lenses/ — nothing to migrate")
        return 0
    leaf_to_table, dialects, ambiguous = _migrate_context(root)
    for leaf in ambiguous:
        print(
            f"warning: leaf table '{leaf}' maps to several entity sources — its "
            "references stay unqualified; qualify those by hand",
            file=sys.stderr,
        )
    total_migrated = 0
    for cases_path in case_files:
        lens_dir = cases_path.parent.parent
        lens = lens_dir.name
        dialect = _lens_dialect_local(lens_dir, dialects)
        cases_text = cases_path.read_text(encoding="utf-8")
        cases = yaml.safe_load(cases_text) or []
        if not isinstance(cases, list):
            print(f"error: {cases_path} is not a top-level YAML list — fix it first")
            return 1
        cert_path = lens_dir / "certified_answers.yaml"
        cert_text = cert_path.read_text(encoding="utf-8") if cert_path.exists() else ""
        certified = yaml.safe_load(cert_text) or []
        if not isinstance(certified, list):
            print(f"error: {cert_path} is not a top-level YAML list — fix it first")
            return 1
        have = {str(a.get("question", "")).strip().lower() for a in certified}
        fresh: list[dict[str, object]] = []
        kept: list[dict[str, object]] = []
        skipped: list[str] = []
        for case in cases:
            question = str(case.get("question", "")).strip() if isinstance(case, dict) else ""
            if not isinstance(case, dict) or not case.get("expected_sql"):
                kept.append(case)  # behavioral / question-only — stays an eval case
                continue
            if question.lower() in have:
                skipped.append(question)  # already certified — the case just retires
                continue
            status = str(case.get("status") or "candidate")
            if status != "approved":
                # Certified answers SERVE on match — promoting unreviewed SQL
                # into the corpus is exactly what review exists to prevent.
                kept.append(case)
                print(
                    f"{lens}: kept '{question}' as an eval case — status is "
                    f"'{status}', and only approved cases migrate (approve it, "
                    "or certify by hand)"
                )
                continue
            sql = str(case["expected_sql"])
            if dialect is None:
                print(
                    f"warning: {lens}: no warehouse dialect resolvable from lens.yaml/"
                    f"dst.yaml — '{question}' migrated with its SQL unqualified; "
                    "verify table names before apply",
                    file=sys.stderr,
                )
            else:
                try:
                    # Eval-plane SQL names leaf tables; certified SQL runs live
                    # and needs the physical names (the same rewrite apply's
                    # oracle gate uses).
                    sql = rewrite_to_sources(sql, dialect, leaf_to_table)
                except Exception as exc:  # noqa: BLE001 — keep the original, say why
                    first = str(exc).splitlines()[0][:160]
                    print(
                        f"warning: {lens}: could not qualify '{question}' ({first}) — "
                        "migrated verbatim; qualify table names by hand",
                        file=sys.stderr,
                    )
            if case.get("expected_answer"):
                print(
                    f"note: {lens}: '{question}' had expected_answer — not carried; "
                    "certified answers verify by executing their SQL",
                    file=sys.stderr,
                )
            entry: dict[str, object] = {
                "question": question,
                "sql": sql,
                "source": "evals:migrated",
            }
            if case.get("created_by"):
                entry["verified_by"] = case["created_by"]
            fresh.append(entry)
            have.add(question.lower())
        if not fresh and not skipped:
            print(f"{lens}: no value cases — nothing to migrate")
            continue
        if fresh:
            dumped = yaml.safe_dump(fresh, sort_keys=False, allow_unicode=True)
            if certified:
                cert_out = cert_text.rstrip("\n") + "\n" + dumped
            else:  # keep the comment header, drop the empty `[]`
                head = _comment_head(cert_text)
                cert_out = "\n".join([*head, dumped]) if head else dumped
            cert_path.write_text(cert_out, encoding="utf-8")
        head = _comment_head(cases_text)
        # No live `[]` when nothing is kept — appending after it is malformed
        # YAML (the scaffold dropped its placeholder for the same reason).
        kept_dump = yaml.safe_dump(kept, sort_keys=False, allow_unicode=True) if kept else ""
        cases_out = "\n".join([*head, kept_dump]) if head else kept_dump
        cases_path.write_text(cases_out, encoding="utf-8")
        for question in skipped:
            print(f"{lens}: skipped '{question}' — already in certified_answers.yaml")
        print(
            f"{lens}: migrated {len(fresh)} case(s) into certified_answers.yaml; "
            f"{len(kept)} behavioral kept"
        )
        total_migrated += len(fresh)
    if total_migrated:
        print("review the entries, then `dst apply` to land them")
    return 0


def _rule(args: argparse.Namespace) -> int:
    import httpx

    url, headers = _client(args)
    body = {"verdict": args.verdict, "reasoning": args.reasoning}
    r = httpx.post(
        f"{url}/mgmt/reviews/{args.ticket_id}/rule", headers=headers, json=body, timeout=60
    )
    if r.status_code == 404:
        print(f"no review ticket '{args.ticket_id}'")
        return 1
    t = r.raise_for_status().json()
    print(f"{t['ticket_id']}: ruled {args.verdict} (state={t['state']})")
    if not args.certify:
        return 0
    # --certify: promote the ruled question→SQL in the same act (the UI's
    # "Promote this question→SQL"). The rule response carries lens + request_id.
    # Embedder-less installs still succeed (0027): stored unembedded + a warning.
    r = httpx.post(
        f"{url}/mgmt/lenses/{t['lens']}/certified/from-request/{t['request_id']}",
        headers=headers,
        timeout=60,
    )
    if r.status_code >= 400:
        # The ruling already landed — say so instead of tracebacking past it.
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error bodies happen on 5xx
            detail = r.text[:200]
        print(f"error: ruled, but not certified — {detail}", file=sys.stderr)
        return 1
    cert = r.json()
    print(f"certified {cert['id']} (lens={cert['lens']})")
    if cert.get("warning"):
        print(cert["warning"], file=sys.stderr)
    return 0


def _note_text(args: argparse.Namespace) -> str | None:
    """The correction note: ``--note``, ``--note-file`` (``-`` reads stdin), or
    piped stdin. Corrections are paragraphs — the cold-start agents wrote
    multi-sentence rulings, which is not a shell flag's shape. Prints why and
    returns None when no note can be read."""
    if args.note_file:
        if args.note_file == "-":
            note = sys.stdin.read()
        else:
            path = Path(args.note_file)
            if not path.is_file():
                print(f"error: no note file '{args.note_file}'", file=sys.stderr)
                return None
            note = path.read_text(encoding="utf-8")
    elif args.note:
        note = args.note
    elif not sys.stdin.isatty():
        note = sys.stdin.read()
    else:
        print(
            "error: no correction note — pass --note TEXT, --note-file PATH "
            "(or `-`), or pipe the note in",
            file=sys.stderr,
        )
        return None
    note = note.strip()
    if not note:
        print("error: the correction note is empty", file=sys.stderr)
        return None
    return note


def _correct(args: argparse.Namespace) -> int:
    """`dst correct <request_id> --kind K --target T --note …` — step 3 of
    the documented improvement loop as a verb.

    Filing a correction was REST-only, so every agent running the loop
    hand-rolled an HTTP script to get from a wrong answer to a drafted patch
    (cold-start blocker 11). ``--target`` is REQUIRED here even though the API
    allows it to be absent: without it placement falls back to vocabulary
    matching, which lands the drafted rule on whichever definition page shares
    the most words — and a rule grafted onto the wrong page can regress a
    question that was passing. Opens the review ticket `dst patches draft`
    then drafts from.

    ``--key dst_…`` files AS a caller, and that door is the reason the verb
    matters: the flywheel begins with the person who SAW the wrong answer, and
    that person holds a caller key and nothing else. Resolving credentials as an
    admin verb shut the verb on exactly them, so every wrong answer a business
    user found reached the data team as a message rather than through the
    product. `POST /v1/reviews` had accepted caller keys all along; no CLI path
    reached it with one."""
    import httpx

    url, headers = _client(args, caller_key_ok=True, admin_first=True)
    note = _note_text(args)
    if note is None:
        return 1
    correction: dict[str, object] = {"kind": args.kind, "note": note, "target": args.target}
    if args.corrected_sql:
        correction["corrected_sql"] = args.corrected_sql
    if args.corrected_answer:
        correction["corrected_answer"] = args.corrected_answer
    r = httpx.post(
        f"{url}/v1/reviews",
        headers=headers,
        json={"request_id": args.request_id, "correction": correction},
        timeout=args.timeout,
    )
    if r.status_code >= 400:
        print(f"error: {_detail(r)}", file=sys.stderr)
        return 1
    ticket = r.json()
    if args.json:
        print(json.dumps(ticket, indent=2, ensure_ascii=False))
        return 0
    print(
        f"{ticket['ticket_id']}: correction filed ({args.kind} → {args.target}) "
        f"on {args.request_id} — state={ticket.get('state')}"
    )
    # The next step has to be one the filer can actually take. `patches draft` is
    # an admin verb: printing it at a business user who just filed with a caller
    # key sends them straight back into the door that was shut.
    if args.as_caller:
        print(
            f"next: your data team triages it — check back with "
            f"`dst reviews --key …` (this ticket: {ticket['ticket_id']})"
        )
    else:
        print(f"next: dst patches draft {ticket['ticket_id']}")
    return 0


def _authored_here(root: Path, proposed: str, content: str) -> str | None:
    """The path in THIS project already authoring the asset ``proposed`` would
    create — or None when nothing does and the proposed path stands.

    Delegates to the loader's own discovery rule (``existing_asset_paths``), so a
    writer can never look somewhere the loader doesn't: this used to glob one
    directory while the loader loads a whole subtree, which is exactly how
    ``patches approve`` landed a second ``semantic/definitions/lifetime-value.md``
    beside the scaffold's ``semantic/definitions/examples/`` page. The tree read is
    ``_read_project`` — the same lenses/ + semantic/ scope plan and apply push —
    and the asset directory is part of the identity, so resolving a filename can
    never relocate a ruling into another lens's tree (or out of the shared layer
    ``--shared`` put it in)."""
    from services.project.loader import existing_asset_paths

    return existing_asset_paths(_read_project(root), {proposed: content}).get(proposed)


def _patches(args: argparse.Namespace) -> int:
    """`dst patches list --lens x` / `dst patches draft <ticket_id>` /
    `dst patches approve|reject <id>` — the self-healing loop's ruling from
    the repo. Drafting PRINTS the drafted target and body: the loop requires
    reading the draft before approving (a mistargeted draft, or a wrong
    amendment, is only visible here). Approving a definition/instruction patch
    PROPOSES a file: the server returns it, this writes it into the working
    tree — into the page that already authors the term, whatever it is named —
    and the human reviews it with `git diff` and lands it with `dst apply`.
    Nothing about that ruling is live until they do. Rejecting stores the reason
    (`--note`) on the candidate, which the next draft is bound by."""
    import httpx

    url, headers = _client(args)
    if args.action == "list":
        if not args.lens:
            print("error: `dst patches list` needs --lens <name>", file=sys.stderr)
            return 1
        params = {"status": args.status} if args.status else {}
        r = httpx.get(
            f"{url}/mgmt/lenses/{args.lens}/patches", headers=headers, params=params, timeout=60
        )
        candidates = r.raise_for_status().json()
        if args.json:
            print(json.dumps(candidates))
            return 0
        if not candidates:
            print(f"no drafted patches for lens '{args.lens}'")
            return 0
        for c in candidates:
            print(f"{c['id']}  {c['status']:<10} {c['kind']:<10} {c['target']}")
        return 0

    if not args.id:
        what = "a review ticket id" if args.action == "draft" else "a patch id"
        print(f"error: `dst patches {args.action}` needs {what}", file=sys.stderr)
        return 1

    if args.action == "draft":
        r = httpx.post(
            f"{url}/mgmt/reviews/{args.id}/draft-patch", headers=headers, timeout=args.timeout
        )
        if r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            return 1
        c = r.json()
        if args.json:
            print(json.dumps(c, indent=2, ensure_ascii=False))
            return 0
        print(f"{c['id']}  {c['status']:<10} {c['kind']:<10} {c['target']}")
        if c.get("diff_before"):
            print(f"\n--- current {c['kind']} '{c['target']}' ---\n{c['diff_before']}")
        print(f"\n--- drafted {c['kind']} '{c['target']}' ---\n{c['diff_after']}")
        print(
            f"\nread the target and body above — the draft AMENDS the current body, "
            f"so check the CHANGED ruling, not whether the rest survived; then "
            f"`dst patches approve {c['id']} --dir .` "
            f"or `dst patches reject {c['id']} --note '<why>'`"
        )
        return 0

    if args.action == "reject":
        r = httpx.post(
            f"{url}/mgmt/reviews/patches/{args.id}/reject",
            headers=headers,
            json={"note": args.note} if args.note else None,
            timeout=60,
        )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:  # noqa: BLE001 — non-JSON error bodies happen on 5xx
                detail = r.text[:200]
            print(f"error: {detail}", file=sys.stderr)
            return 1
        out = r.json()
        noted = f" — {args.note}" if args.note else ""
        print(f"{out['id']}: rejected{noted}")
        return 0

    r = httpx.post(
        f"{url}/mgmt/reviews/patches/{args.id}/approve",
        headers=headers,
        params={"shared": "true"} if args.shared else None,
        timeout=120,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error bodies happen on 5xx
            detail = r.text[:200]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    out = r.json()
    print(f"{out['id']}: approved ({out['kind']} {out['target']})")
    proposed = out.get("proposed_file")
    if proposed:
        # Resolved: the default --dir is "." — relative_to across the two
        # spellings raises, so resolve the root once and stay absolute.
        root = Path(args.dir).resolve()
        written = _authored_here(root, proposed["path"], proposed["content"])
        if written is not None:
            print(
                f"note: '{out['target']}' is already authored in {written} — "
                f"patching that file instead of creating {proposed['path']}"
            )
        else:
            written = proposed["path"]
        dest = root / written
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(proposed["content"], encoding="utf-8")
        # next_step tells an API caller to WRITE the file; this just did, so say
        # the half that is still theirs — the ruling is not live until they apply.
        print(f"wrote {written} — NOT live yet: commit it, then run `dst apply`")
    else:
        print(out["next_step"])
    if out.get("eval_case_id"):
        print(f"eval case {out['eval_case_id']} filed (candidate)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dst", description="dst — governed answers over your warehouse"
    )
    from services import __version__ as pkg_version
    from services.build_info import GIT_DIRTY, GIT_SHA

    # Name the build, not just the wheel: two installs of the same version can
    # differ by every unreleased commit, and only /health said which was running
    # (flywheel e2e lap 2). Wheels have no .git — version alone, as before.
    build = f" ({GIT_SHA[:12]}{' dirty' if GIT_DIRTY else ''})" if GIT_SHA else ""
    parser.add_argument("--version", action="version", version=f"dst {pkg_version}{build}")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color (also: NO_COLOR/DST_NO_COLOR env; color is TTY-only anyway)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="run database migrations to head").set_defaults(fn=_migrate)

    p = sub.add_parser(
        "doctor",
        help="can this install actually run? DB schema, embedder, and one cheap "
        "REAL call per model tier — catches a configured-but-uncallable provider "
        "before a query 500s",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env supplies DATABASE_URL and provider keys (default: .)",
    )
    p.set_defaults(fn=_doctor)
    sub.add_parser("secret", help="generate a DST_SECRET_KEY (Fernet)").set_defaults(fn=_secret)

    p = sub.add_parser("rotate-key", help="re-encrypt stored secrets under a new DST_SECRET_KEY")
    p.add_argument(
        "--force",
        action="store_true",
        help="allow rotation with a single configured key (re-encrypt in place)",
    )
    p.add_argument("--dir", default=".", help="project root whose .env supplies the keys")
    p.set_defaults(fn=_rotate_key)

    p = sub.add_parser(
        "bootstrap", help="create (or reuse) an org + mint a fresh admin token; idempotent"
    )
    p.add_argument(
        "--url",
        help="ignored — bootstrap talks to DATABASE_URL directly (no server needed)",
    )
    p.add_argument("--org", default="default")
    p.add_argument("--email", help="create or update the first dashboard admin user")
    p.add_argument("--password", help="its password (omit to be prompted)")
    p.set_defaults(fn=_bootstrap)

    p = sub.add_parser("demo", help="publish the bundled duckdb demo lens into an org")
    p.add_argument("--org-id", required=True, help="org UUID from `dst bootstrap`")
    p.set_defaults(fn=_demo)

    p = sub.add_parser(
        "reindex",
        help="re-embed all stored vectors with the configured embedder "
        "(after an embedding model/dim change); resumable",
    )
    p.add_argument("--batch", type=int, default=64, help="rows per committed batch")
    p.set_defaults(fn=_reindex)

    p = sub.add_parser("init", help="scaffold a new dst project (dbt-style)")
    p.add_argument(
        "dir", nargs="?", default=None, help="target directory (default: create ./<project name>)"
    )
    p.add_argument("--name")
    p.add_argument(
        "--instance-name",
        help="what the org's AI calls this deployment, e.g. watson (default: dst; "
        "written to .env as DST_INSTANCE_NAME and used as the MCP registration name)",
    )
    p.add_argument(
        "--warehouse", choices=["demo", "duckdb", "postgres", "bigquery", "snowflake", "none"]
    )
    p.add_argument(
        "--duckdb-path",
        help="with --warehouse duckdb: path to your .duckdb file "
        "(relative = resolved from the serve cwd)",
    )
    p.add_argument(
        "--example",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the example lens over the bundled duckdb demo (default: yes)",
    )
    p.add_argument("--db-port", type=int, default=5432, help="host port for the project's Postgres")
    p.add_argument(
        "--api-port",
        type=int,
        default=8000,
        help="port dst serve/dev will use (written to .env as DST_URL)",
    )
    p.add_argument("--yes", action="store_true", help="accept defaults, no prompts")
    p.add_argument(
        "--skills-only",
        action="store_true",
        help="refresh an EXISTING project's AGENTS.md and .claude/skills/ from the "
        "installed dst, reporting what changed — the scaffold is a snapshot taken at "
        "init, so a skill improved in a later release never reaches a project otherwise",
    )
    from services.cli.init import run_init

    p.set_defaults(fn=run_init)

    p = sub.add_parser("import", help="one-shot import from a metric layer into semantic/ files")
    p.add_argument("what", choices=["dbt", "osi"])
    p.add_argument(
        "--target-dir",
        help="dbt only: target/ dir holding manifest.json + semantic_manifest.json",
    )
    p.add_argument("--file", help="osi only: an OSI/Ossie semantic model (.yaml or .json)")
    p.add_argument("--connection", required=True, help="dst connection the tables live on")
    p.add_argument("--dir", default=".", help="project root to write semantic/ files into")
    p.set_defaults(fn=_import_metric_layer)

    p = sub.add_parser(
        "query", help="ask a governed question through a lens (the verify one-liner)"
    )
    p.add_argument("lens")
    p.add_argument("question")
    p.add_argument("--json", action="store_true", help="full QueryResponse JSON")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="ask AS a caller: a dst_ caller key from `dst keys create` (default: "
        "DST_API_KEY from env or ./.env). The admin token bypasses every lens "
        "allow-list, so this is the only way to prove an access grant — or a denial",
    )
    p.set_defaults(fn=_query)

    p = sub.add_parser("define", help="print a governed term's meaning — no SQL, no warehouse")
    p.add_argument("term")
    p.add_argument("--json", action="store_true", help="full DefinitionLookup JSON")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="look up AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env); you see terms only from lenses your key may use",
    )
    p.set_defaults(fn=_define)

    p = sub.add_parser(
        "sql",
        help="run read-only SQL against a connection and see the rows — guarded, "
        "row-capped, and logged. For per-column enums/null rates/ranges reach for "
        "`introspect --profile` instead; this is for rows and cross-column facts",
    )
    p.add_argument("sql", help="a single SELECT statement")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--connection", help="probe a whole connection (needs an admin token)")
    scope.add_argument(
        "--lens", help="probe within a lens's allow-list (works with a dst_ caller key)"
    )
    p.add_argument("--limit", type=int, default=20, help="rows to return (default 20, max 500)")
    p.add_argument("--json", action="store_true", help="columns + rows as an object")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--key", help="a dst_ caller key, for --lens (default: DST_API_KEY)")
    p.set_defaults(fn=_sql)

    p = sub.add_parser(
        "introspect",
        help="schema + profile facts for a connection, agent-legible — author "
        "semantic/ files from it with your own agent (see `dst sql` for actual rows)",
    )
    p.add_argument("--connection", required=True)
    p.add_argument("--tables", help="comma-separated table subset")
    p.add_argument(
        "--check-joins",
        action="store_true",
        help="measure each declared join's real cardinality against the warehouse and "
        "report declarations the data contradicts. The compiler trusts `relationship` "
        "to decide whether a join is safe to emit, so a wrong one silently multiplies "
        "every aggregate; exits non-zero when a declaration claims safety the data denies",
    )
    p.add_argument(
        "--profile",
        action="store_true",
        help="sample the warehouse now for enum values, null rates and ranges — "
        "row-capped read-only reads, one pass per table in scope (narrow it with "
        "--tables). Without it the listing is schema only and says so. Applies to "
        "the dst.yaml path — the server always answers with what it stored",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the listing as a parseable object instead of prose",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose dst.yaml declares the connection (no apply needed)",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_introspect)

    p = sub.add_parser(
        "drift",
        help="what changed in the warehouse since this project recorded it, crossed "
        "with the definitions, entities and certified answers that read the tables "
        "that changed — the diff `introspect` does not print (it prints a snapshot) "
        "and the move `dst test` cannot see (both its sides read today's warehouse). "
        "Exit codes gate a nightly run: 0 clean, 2 changes (none breaking), 1 changes "
        "breaking declared references, 4 not armed (run `dst probe`)",
    )
    p.add_argument("--connection", required=True)
    p.add_argument(
        "--accept",
        action="store_true",
        help="re-record the baseline as the warehouse now stands, after reviewing "
        "the findings — the reviewed-it act, so the next run reports what changed "
        "since THIS review and not since the first one",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the findings as a parseable object, each with the semantic "
        "assets that read the changed table",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root: dst.yaml declares the connection, semantic/ is what "
        "the findings are crossed against, profiles/ holds the baseline",
    )
    p.set_defaults(fn=_drift)

    p = sub.add_parser(
        "probe",
        help="record the warehouse's full profile — partitions, freshness, value "
        "dictionaries, crossed with the entities that read each table — into "
        "profiles/<conn>.probe.json; committed, it rides `dst apply` into every "
        "served prompt. Re-run freely (nightly cron is the cadence); `drift` diffs "
        "the schema, this records the truth",
    )
    p.add_argument(
        "--connection",
        help="probe just this declared connection (default: every warehouse "
        "connection dst.yaml declares)",
    )
    p.add_argument(
        "--sample-all",
        action="store_true",
        help="sample every table, not just the ones the semantic layer reads — "
        "one capped read per table, so on a wide warehouse this is the expensive "
        "form (the catalog pass always covers everything either way)",
    )
    p.add_argument(
        "--tables",
        help="comma-separated tables to sample (exact names as introspect prints "
        "them); the catalog pass still records every table",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root: dst.yaml declares the connections, semantic/ is what "
        "the artifact is crossed against, profiles/ receives it",
    )
    p.set_defaults(fn=_probe)

    p = sub.add_parser("dev", help="Postgres up + migrate + serve (auto-reload), one command")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default: the port in DST_URL (.env), else 8000")
    p.set_defaults(fn=_dev, reload=True)

    p = sub.add_parser("serve", help="run the API (+ SPA when built) via uvicorn")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default: the port in DST_URL (.env), else 8000")
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=_serve)

    p = sub.add_parser(
        "test",
        help="run the certified test suite — generation vs certified SQL, full "
        "active corpus; exit 1 on any divergence",
    )
    p.add_argument("lens", nargs="?", help="lens to test (default: every published lens)")
    p.add_argument(
        "--all",
        action="store_true",
        help="test every published lens (the default when no lens is named)",
    )
    p.add_argument(
        "--tag",
        action="append",
        help="run only behavioral cases carrying this tag (repeatable, any-match) — "
        "e.g. --tag intent:discriminator; certified answers carry no tags, so a "
        "tagged run is a case slice",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env supplies DATABASE_URL and the provider keys "
        "(default: .) — point the sweep at a project from outside it",
    )
    p.add_argument(
        "--org",
        help="org to sweep, by name (default: the org this project's "
        "DST_ADMIN_TOKEN authenticates as) — lens names are NOT unique across "
        "orgs, so an unscoped sweep tests every org's lens of that name",
    )
    p.add_argument(
        "--url",
        help="ignored — `test` runs in-process against DATABASE_URL (no server needed); use --dir",
    )
    p.add_argument(
        "--token",
        help="ignored — `test` runs in-process against DATABASE_URL (no server needed); use --dir",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="the same rows as a parseable object — per case: org, lens, question, "
        "verdict, certified/generated result, reason, elapsed_s — plus the summary",
    )
    p.set_defaults(fn=_test)

    p = sub.add_parser(
        "reviews",
        help="list the review queue (all states and origins) — or, with a caller "
        "key, just the tickets on your own requests",
    )
    p.add_argument("--state", help="filter: open|ai_review|needs_human|approved|changes|rejected")
    p.add_argument(
        "--origin",
        choices=["ai", "human"],
        help="filter: 'ai' = auto-flagged by the lens's auto_review policy, "
        "'human' = caller-raised",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--json",
        action="store_true",
        help="machine-readable ticket list on stdout (unfiltered — combine with --state/--origin)",
    )
    g.add_argument(
        "--watch",
        action="store_true",
        help="poll needs_human tickets, print each new one once (id, lens, question); Ctrl-C stops",
    )
    p.add_argument("--interval", type=int, default=30, help="watch poll interval, seconds")
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="read AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env when no admin token is in scope). Lists only the tickets on your own "
        "requests — what happened to the corrections you filed",
    )
    p.set_defaults(fn=_reviews)

    p = sub.add_parser("rule", help="rule on a review ticket")
    p.add_argument("ticket_id")
    p.add_argument("--verdict", required=True, choices=["approve", "changes", "reject"])
    p.add_argument("--reasoning", default="")
    p.add_argument(
        "--certify",
        action="store_true",
        help="after an approve ruling, promote the request's question→SQL to certified",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_rule)

    p = sub.add_parser(
        "correct",
        help="file a correction against a served answer (loop step 3) — opens the "
        "review ticket `dst patches draft` drafts the fix from",
    )
    p.add_argument("request_id", help="the request_id of the wrong answer (printed by `query`)")
    p.add_argument(
        "--kind",
        required=True,
        choices=["definition", "scope", "number", "freshness", "other"],
        help="what kind of wrong it is — the drafter routes on this",
    )
    p.add_argument(
        "--target",
        required=True,
        help="the definition term (or artifact) this correction is about — used "
        "VERBATIM by the drafter; a new term drafts a new definition. Required: "
        "without it placement is vocabulary matching, which mistargets",
    )
    p.add_argument("--note", help="what is wrong, stated decisively (paragraphs: use --note-file)")
    p.add_argument("--note-file", help="read the note from a file, or `-` for stdin")
    p.add_argument("--corrected-sql", help="the SQL that WOULD have been right (strongest signal)")
    p.add_argument("--corrected-answer", help="the answer that would have been right")
    p.add_argument("--json", action="store_true", help="the full ticket JSON on stdout")
    p.add_argument("--timeout", type=int, default=180, help="seconds to wait (the judge runs here)")
    p.add_argument("--dir", default=".", help="project root whose .env supplies url + token")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="file AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env when no admin token is in scope) — the posture of the business user who "
        "found the wrong answer. You may correct your own requests; an admin, anyone's",
    )
    p.set_defaults(fn=_correct)

    p = sub.add_parser(
        "patches",
        help="list a lens's drafted fixes, draft one from a review ticket (prints "
        "the drafted target and body to gate), approve one — approving writes the "
        "proposed file into the project; review it with `git diff`, land it with "
        "`dst apply` — or reject one with a stored reason",
    )
    p.add_argument("action", choices=["list", "draft", "approve", "reject"])
    p.add_argument("id", nargs="?", help="patch id (approve/reject), or review ticket id (draft)")
    p.add_argument("--lens", help="lens whose patches to list")
    p.add_argument("--status", help="filter: candidate|approved|rejected (list)")
    p.add_argument("--note", help="why the draft is declined, stored as drafter feedback (reject)")
    p.add_argument("--json", action="store_true", help="machine-readable list/candidate on stdout")
    p.add_argument("--dir", default=".", help="project root to write the proposed file into")
    p.add_argument(
        "--timeout", type=int, default=180, help="seconds to wait for the drafter (draft)"
    )
    p.add_argument(
        "--shared",
        action="store_true",
        help="approve: land the definition in the shared layer (semantic/definitions/) "
        "instead of this lens's tree — every lens selecting the term gets the ruling",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_patches)

    p = sub.add_parser(
        "observe",
        help="who has been using this layer and what for — usage by caller, recent "
        "requests, one request in full",
    )
    p.add_argument(
        "action",
        nargs="?",
        choices=["callers", "requests", "show"],
        help="omit for the headline + per-caller usage",
    )
    p.add_argument("request_id", nargs="?", help="with `show`: the request to print")
    p.add_argument("--lens", help="restrict to one lens")
    p.add_argument("--status", help="with `requests`: ok | refused | error")
    p.add_argument("--limit", type=int, default=50, help="with `requests` (max 200)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--dir", default=".", help="project root whose .env holds URL and credentials")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_observe)

    p = sub.add_parser(
        "semantic",
        help="manage shared semantic assets on the server: `get` reads back a "
        "published asset (YAML), `rm` deletes one",
    )
    p.add_argument("action", choices=["get", "rm"])
    p.add_argument("kind", choices=["entity", "definition"])
    p.add_argument("name")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="the stored body as JSON")
    p.set_defaults(fn=_semantic)

    p = sub.add_parser(
        "lens",
        help="manage lenses on the server: `list` (deployed lenses, incl. the "
        'server-only ones to adopt), `rm <name>`, `prompt <name> "<question>"` '
        "to see the prompt the model would actually get, or `log <name>` for the "
        "published-version history (who, when, why)",
    )
    p.add_argument("action", choices=["list", "rm", "prompt", "log"])
    p.add_argument("name", nargs="?", help="lens name (not used by `list`)")
    p.add_argument(
        "question",
        nargs="?",
        help="`prompt` only: the question to assemble the generation prompt for",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation (headless)")
    p.add_argument(
        "--json",
        action="store_true",
        help="`prompt`: the full preview JSON; `log`: the raw version rows",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_lens)

    p = sub.add_parser(
        "connection",
        help="rm: delete a server-side connection (dependents checked first) — "
        "the CLI parity for what plan flags as server-only",
    )
    p.add_argument("action", choices=["rm"])
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="skip the confirmation (headless)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_connection)

    p = sub.add_parser("keys", help="create a caller + key, or list callers")
    p.add_argument("action", choices=["create", "list"])
    p.add_argument("--caller", help="caller name (required for create)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_keys)

    p = sub.add_parser("revoke-key", help="revoke a caller's active API keys, in ONE org")
    p.add_argument("--caller", required=True)
    p.add_argument(
        "--org",
        help="the org to revoke in (default: the org this project's DST_ADMIN_TOKEN "
        "belongs to). Caller names are not unique across orgs — this command never "
        "revokes in an org you did not name",
    )
    p.add_argument("--dir", default=".", help="project root whose .env supplies the token")
    p.set_defaults(fn=_revoke_key)

    p = sub.add_parser(
        "revoke-token",
        help="revoke ONE credential by its raw value (dstadm_/dst_/dsto_) — for leaks",
    )
    p.add_argument("token", help="the leaked credential itself, as it appears in the file/log")
    p.add_argument("--dir", default=".", help="project root whose .env supplies DATABASE_URL")
    p.set_defaults(fn=_revoke_token)

    p = sub.add_parser(
        "evals",
        help="eval utilities; `gate <lens>` runs ONE lens's publish gate as a dry run "
        "(the apply code path, nothing published); `migrate` moves value cases "
        "(expected_sql) into certified_answers.yaml — local file rewrite",
    )
    p.add_argument("action", choices=["migrate", "gate"])
    p.add_argument("lens", nargs="?", help="lens name (gate only)")
    p.add_argument("--dir", default=".", help="project root (default: .)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="the decision as JSON")
    p.set_defaults(fn=_evals_dispatch)

    for name, fn, help_ in [
        ("export", _export, "write every lens's file tree into a project dir"),
        ("plan", _plan, "diff a project dir against the server (dry run)"),
        ("apply", _apply, "apply a project dir to the server (files win)"),
    ]:
        p = sub.add_parser(name, help=help_)
        if name == "export":
            # `dst export` keeps its meaning (server -> this project's files);
            # `dst export osi` writes the project's semantic layer OUT to the
            # interchange format. One verb, and the argument says which direction.
            p.add_argument(
                "what",
                nargs="?",
                choices=["osi"],
                default=None,
                help="omit for the lens file tree; `osi` for an OSI/Ossie semantic model",
            )
            p.add_argument(
                "--lens",
                action="append",
                help="export just this lens's tree (repeatable) — the adoption "
                "path for server-only lenses; default: the full project",
            )
            p.add_argument(
                "--yes",
                action="store_true",
                help="skip the confirmation when export would drop comments the "
                "server does not store (headless)",
            )
            p.add_argument(
                "--out",
                help="directory to write the lens tree into (default: the project "
                "dir); for `osi`: file to write (default: stdout)",
            )
            p.add_argument("--name", help="osi only: model name (default: the project dir name)")
            p.add_argument(
                "--dialect",
                default="duckdb",
                choices=["bigquery", "duckdb", "postgres", "mysql", "snowflake"],
                help="osi only: which dialect the emitted expressions are written in",
            )
        if name == "apply":
            p.add_argument(
                "--probe-certified",
                action="store_true",
                help="execute each NEW certified answer once (read-only, row-capped) "
                "to record its verified value — new entries only: answers already "
                "stored are not re-probed (re-author sql, or run `dst test` for a "
                "sweep); opt-in: costs one warehouse query per answer; a probe failure "
                "warns and stores the answer anyway",
            )
            p.add_argument(
                "--json",
                dest="as_json",
                action="store_true",
                help="print the server's raw row array (the pre-2026-08 default) "
                "instead of the summarized report",
            )
            p.add_argument(
                "--require-gates",
                action="store_true",
                help="fail closed: abort the apply (non-zero exit) if any lens "
                "configured for an eval gate had it SKIPPED — empty suite, "
                "provider error, or unreachable warehouse alike; by default a "
                "skipped gate publishes with a warning (CI wants this flag)",
            )
            p.add_argument(
                "--quiet",
                action="store_true",
                help="print only what needs a human: rejected lenses, errors, each "
                "distinct warning once with a count, and the gate footer — a "
                "routine 42-lens apply is a handful of lines instead of hundreds",
            )
            p.add_argument(
                "--allow-failing-cases",
                action="store_true",
                help="publish once past a failing-case gate block WITHOUT editing "
                "eval_gate in the file — audited: requires --reason, which lands in "
                "the version history and the apply row; certified divergences still "
                "block (re-certify in the same push instead)",
            )
            p.add_argument(
                "--reason",
                help="why this red publish is intended (required with "
                "--allow-failing-cases), e.g. 'intended behaviour change: term now "
                "ambiguous — cases reconciled next push'",
            )
        if name in ("plan", "apply"):
            # Both were hard-coded. An apply that outruns the client is a
            # zombie — the operator needs the dial, not a traceback.
            p.add_argument(
                "--timeout",
                type=int,
                default=120 if name == "plan" else 300,
                help=f"seconds to wait for the server (default: {120 if name == 'plan' else 300})",
            )
        if name == "plan":
            p.add_argument(
                "--full",
                action="store_true",
                help="full per-file output with diffs (the pre-2026-08 default); "
                "without it plan prints the summarized rows + counts line",
            )
            p.add_argument(
                "--json",
                dest="as_json",
                action="store_true",
                help="print the plan as a parseable list instead of prose, including "
                "the `scope: warehouse` entry — that is where a warehouse the check "
                "could not reach is readable, since the human path stays silent on it",
            )
        p.add_argument("--dir", default=".")
        p.add_argument(
            "--url",
            help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
        )
        p.add_argument(
            "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
        )
        p.set_defaults(fn=fn)

    args = parser.parse_args()
    if args.no_color:
        style.set_enabled(False)
    if getattr(args, "certify", False) and args.verdict != "approve":
        parser.error("--certify requires --verdict approve")
    try:
        return int(args.fn(args))
    except Exception as exc:  # noqa: BLE001 — narrowed in _unreachable_exit; the rest re-raises
        handled = _unreachable_exit(exc, args)
        if handled is None:
            raise
        return handled
    finally:
        # A --dir verb reconfigures this process for the project it was pointed at
        # (_adopt_project_env). One invocation, one adoption: put it back, so nothing
        # a verb resolved outlives the verb in a process that isn't one-shot.
        if restore := getattr(args, "restore_project_env", None):
            restore()


if __name__ == "__main__":
    sys.exit(main())
