"""`dst init` — dbt-style guided project scaffolding.

Interactive by default; every prompt has a flag so CI/scripts run it headless.
Writes: dst.yaml (providers + connection declarations with
DST_API_KEY_<NAME> env refs), .env (generated secret key, DB URLs, one
placeholder line per declared secret), .gitignore, lenses/ with a reference
lens tree, README.md, and `git init`s the directory.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

WAREHOUSES = ("demo", "duckdb", "postgres", "bigquery", "snowflake", "none")


def agent_files(root: Path, name: str, api_port: int) -> list[tuple[Path, str]]:
    """The agent-facing scaffold — AGENTS.md and the six skills — as
    ``(path, content)``.

    Every one is a pure function of the INSTALLED dst (nothing project-specific
    but the name and the port), which is what makes them refreshable: a project
    scaffolded before a skill improved keeps the old copy forever otherwise, and
    nothing in plan/apply/upgrade says so. One list, so the
    scaffold and `dst init --skills-only` can never write different sets."""
    skills = root / ".claude" / "skills"
    return [
        (root / "AGENTS.md", _agents_md(name, api_port)),
        (skills / "dst-semantic" / "SKILL.md", _semantic_skill()),
        (skills / "dst-certify" / "SKILL.md", _certify_skill()),
        (skills / "dst-context" / "SKILL.md", _context_skill()),
        (skills / "dst-history-bootstrap" / "SKILL.md", _history_bootstrap_skill()),
        (skills / "dst-flywheel" / "SKILL.md", _flywheel_skill()),
        (skills / "dst-warehouse-review" / "SKILL.md", _warehouse_review_skill()),
    ]


def stale_agent_files(root: Path) -> list[str]:
    """Scaffolded agent files that DIFFER from what this dst ships, as project-
    relative paths. Empty when current, when the project never had them, or when
    anything about the check is uncertain.

    Only files that EXIST are compared: a project that deleted a skill chose
    that, and a refresh prompt for a file nobody wants is noise. The check is
    string rendering and a compare — no I/O beyond reading what is already
    there — so it can ride a command an author runs all day."""
    try:
        name, api_port = _project_name(root), _api_port(root)
        return [
            path.relative_to(root).as_posix()
            for path, content in agent_files(root, name, api_port)
            if path.exists() and path.read_text(encoding="utf-8") != content
        ]
    except OSError:  # an unreadable scaffold is not a reason to fail a command
        return []


def stale_agent_note(root: Path) -> str | None:
    """The one line a command shows when the scaffold is behind the package."""
    stale = stale_agent_files(root)
    if not stale:
        return None
    listed = ", ".join(stale[:3]) + (f", +{len(stale) - 3} more" if len(stale) > 3 else "")
    return (
        f"skills: {len(stale)} scaffolded file(s) differ from this dst ({listed}) — "
        "refresh with `dst init . --skills-only`"
    )


def refresh_agent_files(root: Path, name: str, api_port: int) -> int:
    """Rewrite the agent-facing scaffold from the installed dst, reporting what
    changed. The write is unconditional — a local edit is not lost, it is a diff
    in the project's own git, which is where an author reviews it anyway."""
    import difflib

    rows: list[tuple[str, str]] = []
    for path, content in agent_files(root, name, api_port):
        rel = path.relative_to(root).as_posix()
        before = path.read_text(encoding="utf-8") if path.exists() else None
        if before == content:
            rows.append(("=", f"  {rel}"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if before is None:
            rows.append(("+", f"+ {rel}  (new)"))
            continue
        diff = list(difflib.unified_diff(before.splitlines(), content.splitlines(), n=0))
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        rows.append(("~", f"~ {rel}  (+{added} -{removed})"))
    for _kind, line in rows:
        print(line)
    changed = sum(1 for kind, _line in rows if kind != "=")
    if not changed:
        print("\nAlready current with this dst — nothing rewritten.")
        return 0
    print(
        f"\nRefreshed {changed} file(s) from the installed dst. "
        "`git diff` shows the content — including any local edits it replaced."
    )
    return 0


def _ask(prompt: str, default: str) -> str:
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:  # non-tty stdin (CI, scripting agents) — the prompt can never be answered
        print(
            f"error: cannot prompt for {prompt} — stdin is not interactive; "
            "pass --yes to accept defaults (--name/--warehouse override them)",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return raw or default


def _google_key_check(path: Path) -> str | None:
    """None = the key authenticated with Google; a string = why it could not.
    Catches the rotated-key trap at the prompt instead of at first query."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        return "google libraries not installed"
    try:
        creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(path), scopes=["https://www.googleapis.com/auth/bigquery.readonly"]
        )
        creds.refresh(Request())
        return None
    except Exception as exc:  # noqa: BLE001 — offline, malformed, revoked: all just a warning
        return str(exc).split("\n")[0][:160]


def _key_env(name: str) -> str:
    return "DST_API_KEY_" + name.upper().replace("-", "_")


def _project_name(root: Path) -> str:
    """The project's own name for a refresh — dst.yaml's, else the directory's."""
    from services.project.schema import parse_project_yaml

    try:
        return parse_project_yaml((root / "dst.yaml").read_text(encoding="utf-8")).name or root.name
    except (OSError, ValueError):
        return root.name


def _api_port(root: Path) -> int:
    """The port THIS PROJECT'S .env records, and deliberately not the process
    environment's.

    `resolve_env_ref` resolves the shell first, which is right for a credential
    and wrong here: the scaffold's AGENTS.md was rendered from the project's own
    port, so reading an ambient `DST_URL=…` (a CI job, `DST_URL=… dst plan`)
    would render a different file and report a current scaffold as stale — a nag
    that appears only for the operators who target a server explicitly."""
    from urllib.parse import urlparse

    env = root / ".env"
    if not env.exists():
        return 8000
    for line in env.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "DST_URL":
            try:
                return urlparse(value.strip()).port or 8000
            except ValueError:  # a malformed DST_URL is not this command's problem
                return 8000
    return 8000


def run_init(args: argparse.Namespace) -> int:
    interactive = not args.yes
    if getattr(args, "skills_only", False):
        # The one init mode that runs INSIDE a live project: everything else
        # here refuses when dst.yaml exists, and refusing is exactly what left
        # every existing project on its init-time skills.
        root = Path(args.dir or ".").resolve()
        if not (root / "dst.yaml").exists():
            print(f"error: no dst.yaml in {root} — --skills-only refreshes an existing project")
            return 1
        return refresh_agent_files(root, args.name or _project_name(root), _api_port(root))
    if args.dir is None:
        # dbt-style: bare `dst init` creates ./<name> — it never scaffolds into
        # the cwd implicitly, which would strew a project across whatever
        # directory the shell happened to sit in. `dst init .` is the explicit
        # way to use the current directory.
        name = args.name or (_ask("project name", "analytics") if interactive else "analytics")
        root = (Path.cwd() / name).resolve()
        if root.exists():
            print(f"error: {root} already exists; pass a directory explicitly")
            return 1
    else:
        root = Path(args.dir).resolve()
        name = args.name or (_ask("project name", root.name) if interactive else root.name)
    if (root / "dst.yaml").exists():
        print(f"error: {root} already holds a dst project")
        return 1
    warehouse = args.warehouse or (
        _ask(f"warehouse ({'/'.join(WAREHOUSES)})", "demo") if interactive else "demo"
    )
    # The example lens (dbt-style): included by default; the demo warehouse IS it.
    example = args.example
    if example is None:
        example = (
            True
            if warehouse == "demo" or not interactive
            else _ask("include the example lens (bundled duckdb demo)? Y/n", "Y")
            .lower()
            .startswith("y")
        )
    # The name the org's AI addresses this deployment by ("check in watson what our
    # ARR is"). It rides two rails from this one answer: the scaffolded MCP
    # registration command (the client-side alias) and DST_INSTANCE_NAME in .env
    # (the server presents itself by it — FastMCP name + operating manual).
    instance = getattr(args, "instance_name", None) or (
        _ask("what should your AI call this deployment?", "dst") if interactive else "dst"
    )
    db_port = getattr(args, "db_port", None) or 5432
    api_port = getattr(args, "api_port", None) or 8000
    root.mkdir(parents=True, exist_ok=True)

    # ── dst.yaml ──────────────────────────────────────────────────────────
    from services.config import ProviderConfig
    from services.project.schema import ConnectionDecl
    from services.project.template import reference_section

    lines = [f"name: {name}", "", "providers:"]
    lines += [
        "  anthropic:",
        "    type: anthropic",
        f"    api_key_env: {_key_env('anthropic')}",
        "  # add any openai-compatible endpoint (deepseek, ollama, vllm, groq ...):",
        "  # mycheap:",
        "  #   type: openai-compatible",
        "  #   base_url: https://api.example.com",
        f"  #   api_key_env: {_key_env('mycheap')}",
        "  #   fast_model: their-fast-model",
        "  # embeddings power certified-answer matching + routing; three tiers:",
        "  #   poc:        embed: {type: local}   # in-process, no key",
        "  #               (pip install 'dst-core[local-embed]' - or, in a dev",
        "  #               checkout, `uv sync --extra local-embed`; bands auto-adjust)",
        "  #   production: an openai-compatible entry with embedding_model + base_url",
        "  #   none:       omit - generation/guards/evals still work; certified",
        "  #               matching degrades and routing falls back to lexical",
    ]
    # Each reference block sits under the section it documents — appended to the
    # end of the file, the provider fields read as connection config.
    lines += reference_section(
        "Reference: every provider field (under providers.<name>)", ProviderConfig, indent="  "
    ).split("\n")
    connections_key = len(lines)  # patched below when nothing lands under it
    lines += [
        "connections:",
        "  # a real warehouse looks like this (the key is `database`, never `dbname`):",
        "  # warehouse:",
        "  #   type: postgres",
        "  #   config: {host: localhost, port: 5432, database: analytics, user: dst}",
        f"  #   secret_env: {_key_env('warehouse')}   # its password - set it in .env",
        "  # Introspection spans every non-system schema by default; add",
        "  # `schema: <name>` to config to scope it to one.",
    ]
    declared = False  # any connection under `connections:` at all
    secret_envs: list[str] = [_key_env("anthropic")]
    secret_values: dict[str, str] = {}
    if warehouse == "demo" or example:
        # The example lens queries the jaffle connection, so it rides along.
        lines += [
            "  jaffle:",
            "    type: duckdb",
            "    config: {path: fixtures/jaffle_shop.duckdb}",
        ]
        declared = True
        # Self-contained project: the demo warehouse ships with it (the path
        # above resolves against the server's cwd — the project root).
        import shutil

        src = Path(__file__).resolve().parents[2] / "fixtures" / "jaffle_shop.duckdb"
        if src.exists():
            (root / "fixtures").mkdir(parents=True, exist_ok=True)
            shutil.copy(src, root / "fixtures" / "jaffle_shop.duckdb")
            # The demo warehouse is a derivative of dbt Labs' jaffle_shop, and a
            # scaffolded project is meant to be committed and shared — so the
            # attribution has to travel with the file, not stay behind in the
            # package metadata of whoever ran `dst init`.
            notice = src.parent / "jaffle" / "LICENSE"
            if notice.exists():
                shutil.copy(notice, root / "fixtures" / "JAFFLE-LICENSE")
                (root / "fixtures" / "README.md").write_text(
                    "# fixtures\n\n"
                    "`jaffle_shop.duckdb` is the bundled demo warehouse, derived from\n"
                    "dbt Labs' jaffle_shop project (Apache-2.0, modified: schema extended\n"
                    "and data regenerated). Its licence is in `JAFFLE-LICENSE`.\n\n"
                    "Point your lenses at your own warehouse and this directory can go.\n",
                    encoding="utf-8",
                )
        else:
            print(f"warning: demo fixture not found at {src}; fix the jaffle path manually")
    if warehouse == "duckdb":
        # "My warehouse is a DuckDB file" — the warehouse dst bundles as its
        # demo, pointed at real data instead of the fixture.
        duckdb_path = getattr(args, "duckdb_path", None) or (
            _ask("path to your .duckdb file", "warehouse/analytics.duckdb")
            if interactive
            else "warehouse/analytics.duckdb"
        )
        lines += [
            "  warehouse:",
            "    type: duckdb",
            # A relative path resolves against the server process's cwd (the
            # project root under `dst serve`) — said in the file the author reads.
            f"    config: {{path: {duckdb_path}}}  # relative = resolved from the serve cwd",
        ]
        declared = True
        if not (root / duckdb_path).exists() and not Path(duckdb_path).expanduser().is_file():
            print(
                f"note: {duckdb_path} does not exist yet - dst opens it read-only "
                "and never creates it; put the file there before `dst apply`"
            )
    if warehouse not in ("demo", "duckdb", "none"):
        env = _key_env(warehouse)
        cfg = {
            "postgres": "{host: localhost, port: 5432, database: analytics, user: dst}",
            "bigquery": "{project: my-gcp-project}",
            "snowflake": "{account: my-account, warehouse: compute_wh}",
        }[warehouse]
        lines += [
            f"  {warehouse}:",
            f"    type: {warehouse}",
            f"    config: {cfg}",
            f"    secret_env: {env}",
        ]
        declared = True
        secret_envs.append(env)
        # Credentials live somewhere already — ask where, and point the env ref
        # at the file (@path) instead of making the user paste a blob into .env.
        if interactive and warehouse == "bigquery":
            cred = _ask("path to the BigQuery service-account JSON (blank = add to .env later)", "")
            if cred:
                cred_path = Path(cred).expanduser()
                if not cred_path.is_file():
                    print(f"warning: {cred_path} not found - fix the {env} line in .env")
                else:
                    err = _google_key_check(cred_path)
                    if err is None:
                        print("  credentials verified with Google")
                    else:
                        print(
                            f"warning: key failed verification ({err}) - a rotated/old key "
                            "will break at apply; point at the newest key file"
                        )
                secret_values[env] = f"@{cred_path}"
    if not declared:
        # A `connections:` key whose only content is comments loads as null,
        # which the project schema rejects — so `--warehouse none --no-example`
        # would scaffold a project that `dst plan` refuses. An empty mapping is
        # the same slot to fill, and it is valid from birth.
        lines[connections_key] = "connections: {}   # add yours below - it stays a mapping"
    refs = reference_section(
        "Reference: every connection field (under connections.<name>)",
        ConnectionDecl,
        indent="  ",
    )
    (root / "dst.yaml").write_text("\n".join(lines) + "\n" + refs, encoding="utf-8")

    # ── .env + .gitignore ─────────────────────────────────────────────────────
    from cryptography.fernet import Fernet

    env_lines = [
        "# dst secrets - NEVER commit this file (dst.yaml refers to it by env name).",
        f"DST_SECRET_KEY={Fernet.generate_key().decode()}",
        f"DATABASE_URL=postgresql+psycopg://dst_app:dst_app_dev@localhost:{db_port}/dst",
        f"DATABASE_ADMIN_URL=postgresql+psycopg://dst:dst_dev@localhost:{db_port}/dst",
        f"DST_URL=http://localhost:{api_port}",
        f"DST_INSTANCE_NAME={instance}",
        "",
        "# one line per declared provider/connection secret; a value of @/path/to/file",
        "# loads that file's contents (e.g. a service-account JSON):",
    ] + [f"{e}={secret_values.get(e, '')}" for e in dict.fromkeys(secret_envs)]
    env_file = root / ".env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    # Admin token, Fernet key and provider keys all end up in here; a shared dev
    # box is the normal early habitat, so never leave it world-readable.
    env_file.chmod(0o600)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")

    # Local Postgres for `dst dev` - credentials match the .env above.
    (root / "docker-compose.yml").write_text(
        # Explicit project name: compose otherwise derives it from the dir
        # basename, and two same-named checkouts on one host steal each
        # other's containers.
        f"name: dst-{name}\n"
        "services:\n"
        "  db:\n"
        "    image: pgvector/pgvector:pg16\n"
        "    environment:\n"
        "      POSTGRES_USER: dst\n"
        "      POSTGRES_PASSWORD: dst_dev\n"
        "      POSTGRES_DB: dst\n"
        "    ports:\n"
        f'      - "{db_port}:5432"\n'
        "    volumes:\n"
        "      - pgdata:/var/lib/postgresql/data\n"
        "    healthcheck:\n"
        '      test: ["CMD-SHELL", "pg_isready -U dst"]\n'
        "      interval: 5s\n"
        "      timeout: 3s\n"
        "      retries: 10\n"
        "\n"
        "volumes:\n"
        "  pgdata:\n",
        encoding="utf-8",
    )

    # ── reference lens tree + README ──────────────────────────────────────────
    from services.certdefs import CertifiedDefinition
    from services.contracts.lens_config import LensConfig

    defs_doc = (
        "\n## Definition pages\n\n"
        "Each `definitions/*.md` is one governed term: the YAML frontmatter\n"
        "binds it to a metric (and optionally an enforceable `sql` expression);\n"
        "the prose below is the governed meaning - answers cite it. Full\n"
        "frontmatter surface:\n\n```yaml"
        + reference_section("Reference: definition page frontmatter", CertifiedDefinition)
        + "```\n"
    )
    from services.contracts.semantic_model import FIELD_TYPES, Metric
    from services.contracts.shared_semantic import SelectSpec, SharedEntity, SharedJoin

    semantic_doc = (
        "# The shared semantic layer\n\n"
        "(This file is server-ignored; delete it freely.)\n\n"
        "Entities and definitions live HERE, once, at project scope:\n"
        "`semantic/entities/<name>.yaml` and `semantic/definitions/<term>.md`.\n"
        "Folder freely for organization (`entities/sales/deals.yaml`,\n"
        "`definitions/finance/...`) - paths are yours, the asset NAME is the\n"
        "identity and must stay unique project-wide. Demo assets ship under\n"
        "`examples/`; delete those folders when you no longer want them.\n"
        "Lenses SELECT them (`lens.yaml` `select:`); apply compiles the selection.\n"
        "Editing a shared file marks every selecting lens stale - `dst plan`\n"
        "shows it, apply recompiles them. A term defined both shared and\n"
        "lens-locally is an apply ERROR, never a silent override.\n"
        "A definition with `status: ambiguous` makes dst ask which meaning\n"
        "is intended instead of guessing (see definitions/examples/value.md).\n\n"
        "`fields[].type` and `dimensions[].type` are a CLOSED enum of SEMANTIC\n"
        "types - " + " | ".join(FIELD_TYPES) + " - never the\n"
        "warehouse's own type. BIGINT/INT64 -> integer, VARCHAR/TEXT -> string,\n"
        "NUMERIC/DOUBLE -> number, TIMESTAMP_NTZ -> timestamp, STRUCT/ARRAY ->\n"
        "json. `dst introspect` already prints the semantic type for every\n"
        "column (warehouse type in parentheses) - copy that.\n\n"
        "Every entity field:\n\n```yaml"
        + reference_section("Reference: entity file (semantic/entities/*.yaml)", SharedEntity)
        + "```\n\nMetric fields (under metrics:):\n\n```yaml"
        + reference_section("Reference: metric fields", Metric)
        + "```\n\nJoin fields (under joins:, owned by the FK side). Type the join\n"
        'condition key QUOTED - `"on":` - or spell it `condition:`: a bare `on:`\n'
        "is a YAML boolean, so the key loads as True and the join reads as if it\n"
        "had no condition at all (dst repairs that one, but quoting is what\n"
        "you mean):\n\n```yaml"
        + reference_section("Reference: join fields", SharedJoin)
        + "```\n\nWhat a lens can select (lens.yaml select:):\n\n```yaml"
        + reference_section("Reference: select block", SelectSpec)
        + "```\n"
        + defs_doc
    )
    if example:
        from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
        from services.lenses.repo import render_lens_repo
        from services.semantic.files import render_semantic_files

        entities, definitions = jaffle_shared_assets()
        for path, content in render_semantic_files(entities, definitions).items():
            # Demo assets nest under examples/ so they never mix with the real
            # layer - folders are organization only, the asset name is identity.
            out = root / path.replace("/entities/", "/entities/examples/").replace(
                "/definitions/", "/definitions/examples/"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
        (root / "semantic" / "README.md").write_text(semantic_doc, encoding="utf-8")
        bundle = jaffle_customer_value_bundle()
        for path, content in render_lens_repo(bundle).items():
            if path == "lens.yaml":
                content += reference_section("Reference: every lens field", LensConfig)
            if path == "certified_answers.yaml":
                # Teach the shape in place - a stranger's first guess at the
                # format is a wrapper key. Comments are plan-canonicalized.
                # No live `[]` under the comments either: the
                # natural motion is APPENDING an entry, and `[]` followed by a
                # list is malformed YAML. Comment-only parses as present-but-
                # empty - the files-win surface stays managed from day one.
                content = (
                    "# Approved question->SQL pairs, served VERBATIM on a match -\n"
                    "# and each one is a regression test: `dst test` re-asks the\n"
                    "# question and compares against this SQL's RESULT. So the SQL\n"
                    "# must answer the question in the shape the question asks for:\n"
                    "# a 'how many' question wants SELECT count(*), not the rows it\n"
                    "# counts - a row-shaped oracle can never match and the answer\n"
                    "# fails `dst test` forever.\n"
                    "# A top-level LIST - one entry per pair, no wrapper key:\n"
                    "# - question: What was revenue last quarter?\n"
                    "#   sql: SELECT ...\n"
                    "#   source: \"<tool>:<ref> '<title>'\"   # where it was verified\n"
                    "#   verified_by: exec KPI dashboard\n"
                    "# A TEMPLATE covers a question family: {slot} placeholders in\n"
                    "# question/sql, typed by slots; sample_bindings (non-empty) make it\n"
                    "# testable - the first one is the match anchor + eval witness:\n"
                    "# - question: revenue in {period}\n"
                    "#   sql: SELECT ... WHERE d >= {period.start} AND d < {period.end}\n"
                    "#   slots: {period: {type: date_range}}   # date_range|date|enum|number\n"
                    "#   sample_bindings: [{period: 2026-Q2}]\n"
                    "#   status: active           # active | retired - a retired answer\n"
                    "#                            # is never served, matched, or tested,\n"
                    "#                            # but stays here as history. Retire one\n"
                    "#                            # when a definition change makes it wrong\n"
                    "#                            # and the ANSWER is what's outdated.\n"
                    "# DELETING an entry deletes it on apply (files win) and prints a\n"
                    "# deleted count. Answers promoted from review rulings survive file\n"
                    "# absence - they are server-origin. To stop serving one without\n"
                    "# deleting its history, set status: retired instead.\n"
                    + ("" if content == "[]\n" else content)
                )
            if path == "evals/cases.yaml":
                # Same idea: the shape lives in the file, because nothing else
                # teaches it, and guessing the keys costs several attempts - and
                # same append-safety: no live `[]` under the comments.
                content = (
                    "# Eval cases - BEHAVIORAL expectations: response-SHAPE pins the\n"
                    "# gate and `dst test` run alongside the certified corpus.\n"
                    "# A top-level LIST - one entry per case, no wrapper key:\n"
                    "# - question: what is the average value of a customer?\n"
                    "#   expect: clarify          # or refuse or answer - the required shape\n"
                    "#   term: value              # optional: the term a clarify must name\n"
                    "#   status: approved         # candidate | approved | retired\n"
                    "# expect: answer is the must-answer pin - a question the lens MUST\n"
                    "# answer with data; it catches a lens regressing into refusing\n"
                    "# answerable questions. Pin one for each question a caller relies on\n"
                    "# that is NOT certified (certified answers are already regression-run).\n"
                    "# Do NOT write value cases (expected_sql oracles) here - they are not\n"
                    "# scored: certified answers ARE the regression suite, so certify the\n"
                    "# question in certified_answers.yaml instead. A legacy cases.yaml\n"
                    "# with value cases converts via `dst evals migrate`.\n"
                    + ("" if content == "[]\n" else content)
                )
            out = root / "lenses" / bundle.config.name / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
    else:
        # No example to read the surface from - keep it discoverable anyway.
        (root / "semantic" / "entities").mkdir(parents=True, exist_ok=True)
        (root / "semantic" / "definitions").mkdir(parents=True, exist_ok=True)
        (root / "semantic" / "README.md").write_text(semantic_doc, encoding="utf-8")
        (root / "lenses").mkdir(parents=True, exist_ok=True)
        (root / "lenses" / "REFERENCE.md").write_text(
            "# Lens reference\n\n(This file is server-ignored; delete it freely.)\n"
            "\nA lens lives at `lenses/<name>/` with `lens.yaml` (policy + `select:`\n"
            "over the shared layer), `queries.yaml` (use_when + sample_queries),\n"
            "`definitions/*.md` (lens-local terms only), `certified_answers.yaml`\n"
            "(frozen pairs AND {slot} templates - the file's header comments teach\n"
            "both shapes), `evals/cases.yaml`. Shared entities/definitions live\n"
            "under `semantic/`.\n"
            "\nEvery `lens.yaml` field:\n\n```yaml"
            + reference_section("Reference: every lens field", LensConfig)
            + "```\n"
            "\n## Eval cases (`evals/cases.yaml`)\n\n"
            "BEHAVIORAL expectations only - response-SHAPE pins run through the\n"
            "real pipeline by the gate and `dst test`. A top-level LIST, one\n"
            "entry per case, no wrapper key. Keys: `question`,\n"
            "`expect: clarify | refuse | answer` (the shape the response must\n"
            "have; `answer` is the must-answer pin - refusal measured in both\n"
            "directions, for relied-on questions that are not certified),\n"
            "optional `term` (the term a clarify must name), `status` (candidate |\n"
            "approved | retired; only `status: approved` counts). Do NOT write\n"
            "value cases (`expected_sql` oracles) - they are not scored: certified\n"
            "answers ARE the regression suite, so certify the question in\n"
            "`certified_answers.yaml` instead. A legacy cases.yaml with value\n"
            "cases converts via `dst evals migrate`.\n",
            encoding="utf-8",
        )
    for dest, body in agent_files(root, name, api_port):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    (root / "README.md").write_text(
        f"# {name}\n\nA dst project. Quickstart "
        "(**already running somewhere? skip `dst dev`** - check `DST_URL` in\n"
        f".env / `curl $DST_URL/ready` first, so a second server does not race\n"
        "the live one):\n\n"
        f"```bash\ndst dev                # DB up + migrate + serve on :{api_port}\n"
        "dst bootstrap --org me # once: mints the admin token, saves it to .env\n"
        "dst apply              # files -> server: connections + layer + lenses\n"
        'dst query <lens> "..." # ask a governed question from the terminal\n'
        "dst keys create --caller alex  # one key per PERSON, never per tool\n```\n\n"
        "Every command talks to whatever `DST_URL` says - the process env first,\n"
        f"then `.env`, where init wrote `http://localhost:{api_port}`. If something is\n"
        "already serving (a deployment, another session) that value is already\n"
        "correct and `dst dev` is not the first step; read it, do not guess a\n"
        "port.\n\n"
        "Then grant the caller in `lenses/<name>/lens.yaml` -\n"
        "`access.allow: [{caller: alex}]` - or `[{group: everyone}]` for the whole\n"
        "org (any valid key) - and `dst apply` again. Callers are people;\n"
        "agents ask on their behalf, so every answer stays attributable to\n"
        "whoever the question was really for.\n"
        'Query via `POST /v1/lenses/<lens>/query {"q": "..."}`, or connect an\n'
        "agent over MCP (endpoint `/mcp`, bearer = the caller key):\n\n"
        "```bash\n"
        f"claude mcp add {instance} http://localhost:{api_port}/mcp --transport http \\\n"
        '  --header "Authorization: Bearer <CALLER-KEY>"\n'
        "```\n\n"
        f'The registration name is how people invoke it ("check in {instance} ...");\n'
        "it matches `DST_INSTANCE_NAME` in `.env`, which the server presents\n"
        "itself by - change both to rename.\n\n"
        "Shared entities/definitions live under `semantic/` (edited in ONE place);\n"
        "lenses under `lenses/<name>/` select them + own policy and local extras.\n"
        "Edit files, `dst plan` (summary + counts; --full for diffs, and it\n"
        "names which lenses go stale), apply.\n"
        "Author the layer from `dst introspect --connection <name> --profile`\n"
        "(schema + profile facts: row counts, enum values, null rates, ranges;\n"
        "`--json` for the parseable form, no flag for schema only) or import\n"
        "one-shot from dbt artifacts (`dst import dbt`, never re-synced).\n"
        "Run `dst probe` after authoring and nightly (cron it): it records the\n"
        "warehouse's full profile in `profiles/<conn>.probe.json` - value\n"
        "dictionaries, partitions, freshness - and `dst apply` lands it in the\n"
        "serving prompt, so generation filters on real literals, not guessed ones.\n"
        "Bootstrap certified answers from your BI tool's verified queries\n"
        "(the `.claude/skills/dst-certify/` skill walks it), or bootstrap the\n"
        "whole layer from 30 days of query history\n"
        "(the `.claude/skills/dst-history-bootstrap/` skill walks that).\n"
        "When answers are wrong, run the improvement loop\n"
        "(the `.claude/skills/dst-flywheel/` skill: measure with repeats,\n"
        "correct with an explicit target, gate the draft, re-measure, certify).\n"
        "Secrets live in `.env` (gitignored); dst.yaml refers to them by env name.\n"
        "\nFull documentation: https://www.dataservetool.com\n",
        encoding="utf-8",
    )
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    print(f"initialized dst project '{name}' in {root}/")
    print(f"  warehouse: {warehouse}")
    cd = f"cd {root.name} && " if args.dir is None else ""
    print(f"  next: {cd}fill the DST_API_KEY_* lines in .env, then `dst dev`")
    return 0


def _semantic_skill() -> str:
    """The scaffolded Claude Code skill: the introspect -> author -> select ->
    apply -> verify loop, procedural and on-demand (AGENTS.md stays the ambient
    guide; file shapes live in semantic/README.md - the skill points, never
    duplicates)."""
    return """---
name: dst-semantic
description: Use when asked to author, extend, or fix this project's semantic
  layer from a connected warehouse - turns dst introspect output into
  semantic/ entities and definitions plus lens selections, then applies and
  verifies.
---

# Author the semantic layer from a warehouse

1. Get the raw material (schema + profile facts, agent-legible):

   dst introspect --connection <name> --profile > context.txt

   This is step 1 for a reason and it needs NO prior apply - it reads the
   connection straight out of dst.yaml. Every non-system schema is
   searched and names come back qualified (spider.player); scope the scan
   with `datasets: [finance_marts, product_marts]` (or singular
   `schema: <name>`) under the connection's config, or the listing with
   --tables a,b. SCOPE WIDE WAREHOUSES: unscoped, introspection and
   `dst probe` span every non-system dataset, and a large unrelated dataset
   can eat the whole catalog budget (the semantic layer's own tables are
   always backfilled into the probe, but everything else you care about
   competes for the cap). Empty output is an error with the schemas it searched, never
   a blank line. `--profile` samples the warehouse right there for the facts
   the schema alone cannot give you: enum values, null rates, ranges. The
   reads are row-capped and read-only, but it is one pass per table in scope -
   on a wide warehouse pass --tables. Without it the listing is schema only
   and says NOT PROFILED at the top - it never passes a bare schema off as
   complete.
   In a warehouse whose status column holds 'A'/'C'/'X', those codes ARE the
   business knowledge; author definitions/dimensions from them.
   Table and column DESCRIPTIONS ride the listing too (the ` - <text>`
   suffixes) - the data team's own words, so mine them for definitions and
   dimensions. Do not copy them verbatim into YAML: a blank description
   falls back to the warehouse comment at serve time, so author only what
   the comment does not already say.
   One column per line as `- <name>: <type> (<warehouse type>)`; add --json
   if you would rather parse than read.

   Then RECORD those facts for the project, not just this terminal:

   dst probe

   writes profiles/<conn>.probe.json - the same passes plus partitions and
   freshness, crossed with the entities that read each table. Commit it: the
   next `dst apply` lands it in the serving prompt, so generation filters
   on literals the warehouse actually holds ('FI') instead of guessing
   formats ('Finland'). Re-run it whenever the warehouse moves - a nightly
   cron is the intended cadence. Sampling covers the tables the layer reads
   (everything while the layer is still empty); on a wide warehouse that
   default is the cheap form - `--sample-all` or `--tables a,b` widen or pin
   it, and the catalog pass records every table either way.
   The dictionaries do double duty at serve time: a filter literal outside a
   complete dictionary is repaired before execution, and a zero-row result
   probes the filtered column once before serving - so a stale artifact is
   not just a stale prompt, it weakens a guard. Keep the cron honest.

1b. When a profile fact is not enough - "is a refund a negative amount or a
   row with status='refunded'?" - look at the rows:

   dst sql "SELECT order_id, status, amount FROM orders" --connection <name> --limit 5

   SELECT-only, row-capped, and logged to the audit trail, so the probe behind
   a business rule is evidence rather than a private detour. Use it for rows,
   cross-column facts and join checks; --profile already gives per-column enum
   values, null rates and ranges in one pass, so do not re-derive those here.
   Do NOT open the warehouse client yourself - dst holds that credential
   so you do not have to, and SQL run around it is ungoverned and unlogged.
   (This verb runs server-side, so the connection must be applied; introspect
   reads dst.yaml directly and works before the first apply.)

2. Author from it - file shapes are documented in semantic/README.md:
   - semantic/entities/<entity>.yaml - one per business object: grain, source
     (connection + table), fields, dimensions, metrics (type simple | ratio |
     derived, filters, format, default_time_field), FK-side joins with
     relationship. Relationships live HERE, as joins with a declared
     relationship - a join described in definition prose is a smell: the
     compiler cannot enforce it and every query re-derives it. Column-qualify
     every expression (entity.column, never a bare column) - the validator
     rejects ambiguity late, at apply. `fields[].type` is a closed SEMANTIC
     enum (string | number | integer | boolean | timestamp | date | json),
     never the warehouse type - copy the type introspect prints, and leave
     the parenthesised warehouse type behind.
   - semantic/definitions/<term>.md - what business words mean. Bind meaning
     to structure with about: <entity>.<member>, and make it ENFORCEABLE
     with `sql: <expression>` in the frontmatter (alias sql_expr) - without
     it the definition is prose-only and the definition_applied check can
     never verify an answer against it. A genuinely contested term
     gets status: ambiguous + possible_mappings, so dst asks the user
     which meaning is intended instead of guessing. Write definitions in
     DECISIVE form - the dst-context skill holds the authoring rules
     (exact bindings, traps in negative form, value shapes).

3. Select into a lens - nothing flows automatically, selection is curation:
   lenses/<name>/lens.yaml, under select.entities (bare name = the whole
   entity; add metrics: [..] to subset) and select.definitions (explicit
   list; new terms must be added here).

4. Land it:

   dst plan     # summary + counts (--full for per-file diffs); names stale lenses
   dst apply    # upsert + recompile; read the report's errors/warnings

   plan validates what apply validates and exits 1 if any file would be
   rejected - check the exit code, not just the diffs.

   Apply probes warehouse credentials before accepting them - a dead key
   never replaces a working one; the error names the env ref to fix.

5. Verify with one real question per new metric/term:

   dst query <lens> "the question"

   Check the answer, the sql line, and definition_used. For anything
   load-bearing, ask 2-3 times: generated SQL wobbles run to run, and a
   single rep reads as certainty when it is a coin flip.

Gotchas: a metric filter must sit on the exact metric the question uses
(avg_clv vs total_clv); entities say what exists and how it computes,
definitions say what words mean; enforceable SQL belongs on entities.
"""


def _context_skill() -> str:
    """The scaffolded context-authoring skill: how to WRITE definitions and
    context so accuracy goes up instead of sideways - vocabulary prose
    de-inhibits, decisive definitions correct."""
    return """---
name: dst-context
description: Use when writing or reviewing this project's business context -
  semantic/definitions/*.md, lens instructions, context docs. Encodes the
  authoring rules: decisive definitions with exact tables and traps raise
  accuracy; vocabulary-only prose lowers it.
---

# Write context that raises accuracy

A decisive definitions page is the single biggest accuracy lever a lens has.
A vocabulary-only glossary in the same slot gains nothing and CONVERTS safe
declines into confident wrong answers: the model stops declining, but nothing
told it which reading is right. The difference is entirely in how the context
is written.

The organizing principle: RAILS ATTACHED TO THE DATA GENERALISE ACROSS
PHRASING; RAILS ATTACHED TO THE QUESTION DO NOT. A population_filter holds for
every paraphrase and under adversarial prompting, because the compiler ANDs it
in regardless of what the question said; a declared ambiguity keyed to question
wording misses the paraphrases you did not think of. When both homes exist for
a rule, pick the data-side one.

0. The deterministic rails come FIRST - use them before writing any prose.
   On the entity:
   - population: one sentence saying who/what the rows cover; the serve
     check requires answers to carry the scope.
   - population_filter: a SQL predicate the COMPILER ANDS INTO EVERY QUERY
     against the entity - the one scope bound no phrasing can smuggle past.
     ("Active paying accounts only" as description prose is advisory;
     population_filter: "t.is_active_paying = TRUE" is enforced.)
   - pinned_dimensions: dimensions that must be pinned or GROUPed before
     any aggregate - the structural form of "never sum across currencies".
   A rule in description prose steers generation NON-deterministically -
   obeyed for one phrasing, violated for the next - and apply warns about it;
   these fields are where those rules belong.

1. Decide, don't describe. Every definition names its exact table/column and
   the computation: "Overdue = outstanding in every aging bucket except
   'Not due' (equivalently days_overdue > 0), on silver.fct_ar_aging."
   A meaning without a binding ("Aging: receivables grouped by time since
   due date") makes the model switch from robust predicates to fragile
   enumerations - the same question then answers correctly one run and
   wrongly the next.

   AND ENCODE THE BINDING, not only the prose: the frontmatter `sql:` key
   (alias `sql_expr:`) carries the enforceable expression -
   `sql: days_overdue > 0`. Without it the definition is prose-only: the
   definition_applied check SKIPS ("no enforceable definitions"), answers
   cannot be verified against the meaning, and apply warns after the fact.
   A real project authored its whole layer without it and every answer
   graded on partial evidence. Prose explains; `sql:` enforces - write both.

2. Name the trap in negative form. Write the prohibition, not the hope:
   "never a subset of buckets", "gold.rep_leaderboard excludes Renewals - do
   not use it for company-level numbers." Models drift into exactly the paths
   you don't forbid.

3. Contested terms decide or ask - never just define vocabulary. If "revenue"
   means net invoiced to finance and bookings to sales, either write the
   per-caller rule (so each audience gets its own reading) or mark the term
   status: ambiguous with possible_mappings so dst clarifies.
   Vocabulary alone de-inhibits: the model stops declining and guesses a
   third meaning. A wrong answer is worse than no answer.

   The clarify TRIGGER is a literal word-boundary match on the term plus its
   aliases - a phrasing containing neither is NOT caught by the rail (the
   answer then disclosing which reading it used is the floor beneath it).
   Three sub-rules, each of which costs a project real time when missed:
   - Aliases name the AMBIGUITY, not the domain. A bare "attainment" alias on
     a shared sales_attainment definition makes every question in a lens whose
     whole subject is attainment trip the clarify - and the lens answers
     nothing. Same trap as rule 10, biting through the alias field.
   - Mapping LABELS are the escape hatch: a question naming a label serves
     that reading directly. Labels must be words a user would type -
     "contracted-ARR-vs-goal" catches nothing a person types; bare
     "contracted" / "go-live" does.
   - Mapping TAILS ("meaning - entity.column") power the disclosure floor:
     name the actual column/table so a served answer can say which reading
     it used even when the clarify never fired.

4. Certify the hot metrics instead of explaining them. A certified answer is
   worth more than more prose, AND it is cheaper and faster - serving beats
   generating.

5. Small and scoped beats complete - and trim per model tier. Context rides
   EVERY query. Once decisive definitions exist, the bulky profiled-dictionary
   chunk usually stops earning its cost on a strong model - but a weaker
   fast-tier model leans on the profile's enum values and loses questions
   without it. Scope the lens to the tables the definitions name; keep the
   profile chunk for the model tiers that need it, and check by re-measuring
   your own lens rather than by assuming.

6. Document VALUE SHAPES, not just semantics. A VARCHAR column can hold JSON
   objects ({"en": "Abakan", "ru": ...}), point strings ('(lon,lat)'), or
   numbers-as-text - say so and give the access pattern (json_extract_string,
   split_part, CAST). Without the shape rule, generation compares raw JSON to
   a plain string, gets NULL, serves it as the answer and then asserts the
   data does not exist. One shape sentence prevents the whole family.

7. Verify the doc against the warehouse before landing it: every table,
   column, and enum value it names must exist verbatim (dst introspect
   --profile, which is where enum values come from).
   A definition that misnames a bucket label plants the trap it should
   remove.

8. Pin what must not wobble. Generated SQL varies run to run even at
   temperature 0 - the same lens can grade correct on one rep and wrong on
   the next for an identical question. Definitions make answers derivable;
   only a certified answer makes them stable. Certify the questions whose
   numbers leave the company.

9. A SHARED definition's blast radius is every lens that selects it. A rule
   learned from one question family silently rewires siblings: a "render
   periods as first-day dates" convention, correct for monthly questions,
   turns every YEAR grouping in a sibling lens into a January date - right
   values, wrong grid. Scope conventions explicitly (name the carve-outs) and
   after editing a shared definition re-measure EVERY lens selecting it, not
   just the one you were fixing.

10. A rule for one question family gets its OWN term - never graft it onto a
    shared definition as a blanket rule. A "no padding" rule meant for
    growth-rate questions, written into the shared net-amount definition,
    breaks every question that needs padding. New family, new
    definitions/<term>.md, selected into the lens explicitly.

11. Definitions cannot beat the question - the question wins over the lens
    default, by design. For a question phrasing that misleads, write an
    INTERPRETATION guide ("the phrase 'only non-zero volumes are used' means
    the change is undefined when the prior day is zero - it does not mean
    scan back"), not a contradiction the model must ignore. A family that
    stays genuinely ambiguous after that is certification's job, not more
    prose.

12. Mechanics vs meaning: warehouse dialect idioms (DuckDB DATE + BIGINT
    does not bind - cast the series index) belong in lens INSTRUCTIONS;
    business meaning belongs in definitions. The serving default already
    carries the generic output contract (named quantities only, question's
    grain, full precision) - add only your domain's grain rules on top.

13. Never write REAL (or bare FLOAT) into a definition that computes a ratio.
    REAL is 4-byte single precision in DuckDB, Postgres and Snowflake, so the
    same division comes out differently: 2000/2465 is 0.8113590263691683 as
    DOUBLE and 0.8113590478897095 as REAL. A page that says "force real
    division, write CAST(... AS REAL)" forks identical questions onto the
    float32 value, so the same question answers two different numbers. To
    force non-integer division write the literal as `100.0`, or
    CAST(... AS DOUBLE); to force exactness use NUMERIC/DECIMAL. dst's
    guard widens a REAL cast to DOUBLE before serving, so a definition that
    still says REAL is not wrong on the wire - it is just misleading everyone
    who reads it, including the next model you ask to extend it.

Land changes with dst plan / apply; the certified suite and behavioral
pins are the regression net for what you wrote.
"""


def _certify_skill() -> str:
    """The scaffolded BI-import skill: turn a BI export's verified
    queries into certified_answers.yaml entries with provenance. Slots and
    gates only - dst's apply gates are the safety net, not this text."""
    return """---
name: dst-certify
description: Use when asked to import verified BI queries (Looker/Metabase/
  Tableau exports, or plain SQL files) as certified answers, or to bootstrap
  this project's certified layer - turns dashboard tiles into
  certified_answers.yaml entries with provenance, then applies and verifies.
---

# Import verified BI queries as certified answers

1. Locate the export: LookML files, Metabase cards JSON, Tableau workbook
   XML, or plain .sql files. Only queries a human already vouches for
   qualify - scratch queries are not certified answers.

2. For each verified query, one entry in
   lenses/<lens>/certified_answers.yaml:
   - question: rephrase the tile/report title as the question a human
     actually asks ("MRR by segment" -> "what is monthly recurring revenue
     by customer segment?").
   - sql: verbatim from the export, except dialect fixes for the lens's
     warehouse. Never "improve" the query - verification covered THAT sql.
   - source: where it came from, convention "<tool>:<ref> '<title>'"
     (e.g. looker:dashboards/42 'MRR by segment').
   - verified_by: the dashboard/team/person that vouches (a person, "exec
     KPI dashboard", a ticket).

3. Parameterized tiles: author ONE template covering the family instead of
   freezing the default. The question and sql carry {slot} placeholders,
   `slots` types each one, and `sample_bindings` (required, non-empty) make
   it testable - the FIRST binding is the tile's default parameterization
   (it becomes the match anchor and the eval witness):

       - question: revenue in {period}
         sql: >
           SELECT SUM(amount_eur) FROM orders
           WHERE closed_at >= {period.start} AND closed_at < {period.end}
         slots:
           period: {type: date_range}
         sample_bindings:
           - {period: 2026-Q2}

   Slot types: date_range ({name.start}/{name.end} in SQL, half-open;
   values YYYY | YYYY-Qn | YYYY-MM | YYYY-MM-DD/YYYY-MM-DD), date, number,
   enum (inline `values` list, required). Parameterize ONLY what the tile's
   own parameters vary - never widen the approved shape. Phrase the question
   so it reads naturally for EVERY sample value ("with more than {n} orders"
   reads fine at 5 but clunky at 1 - the rendered question is what the eval
   suite asks). A tile whose parameterization you cannot express in these
   types: import the default as a frozen pair, or skip it honestly.

4. Land it:

   dst plan
   dst apply

   Apply gates reject unparseable SQL and any answer referencing tables
   outside the lens's model - the safety net; read the errors, fix the
   entry, re-apply. Optionally `dst apply --probe-certified` executes
   each new answer once and records its verified value.

5. After a bulk import, run the lens's evals / `dst reviews` before
   trusting router matches.

## The other source: the lens's OWN verified answers

BI exports are not the only feedstock. When a generated answer has been
verified correct (execution-graded, human-checked, or oracle-matched), that
answer's SQL is certifiable TODAY - source: the request id, verified_by:
whoever or whatever verified it. Certifying turns a question that was a
coin-flip into a fixed answer served without generation, and it lifts the
UNCERTIFIED neighbors too: nearby questions start serving with certified
exemplars injected (the assisted tier). Certify EARLY, as a mid-loop ratchet
after each verified win - not as a final polish. The question text does not
need to be the user's exact phrasing: matching is by meaning (embedding +
paraphrase gate), so one well-written question covers its whole family.

## Verify the corpus MATCHES, not just that it applied

"Applied" is not "matchable" - a corpus can be active and invisible.
After landing:

1. Read the apply output for "N certified answers have no embedding" - if it
   appears, run `dst reindex` and re-check.
2. Ask ONE certified question verbatim through the lens and check the
   response says certification=certified. If it generated instead, the
   corpus is not matching - fix before certifying more.
3. `dst test` runs generation-vs-certified for the corpus - the standing
   regression net.

The FIRST certified apply an install ever does also loads the embedding
model - it can take minutes. Use `dst apply --timeout` if needed; a
client timeout means the apply is still running and will commit (it holds
the org apply lock) - poll `dst plan` until the diff clears.

Gotchas: certified answers are served VERBATIM - they bypass generation, so
a wrong imported query is a certified wrong answer. source/verified_by are
the audit trail - always fill them. When the shared layer changes,
`dst plan` flags touching answers for re-verify. Certifying is also
writing the regression test - `dst test` runs the certified corpus.
"""


def _flywheel_skill() -> str:
    """The scaffolded improvement-loop skill: measure with repeats, correct
    through the review API with an explicit target, gate the draft, re-measure
    everything, certify verified wins."""
    return """---
name: dst-flywheel
description: Use when a lens answers questions wrongly and you want to
  improve it systematically - the loop: diagnose from traces, file
  corrections with an explicit target, gate the drafted patch, apply,
  re-measure everything, certify verified wins.
---

# The improvement loop

One lap = measure, diagnose, correct, gate, apply, re-measure, certify. A lens
that starts out answering half its questions is normally a handful of laps
away from answering nearly all of them; the laps are cheap, guessing is not.

1. Measure with REPEATS (2-3 per question) and keep request_ids. A single
   rep is a coin flip dressed as a verdict; per-question pass RATES are the
   signal. Grade by executing, not by eyeballing prose. Send an
   `X-Dst-Agent: benchmark` header (or any label) on measurement traffic so
   the runs are attributable in the audit trail and the governed KPI rollup
   is not permanently mixed with your own benchmarking.

2. Diagnose each miss from its trace and SQL before touching anything.
   The classes repeat: wrong VALUES (missing rule -> definition), right
   values in the wrong GRID (grain/projection -> conventions or the answer
   contract), DECLINE (guard rejection or dialect idiom -> lens
   instructions), and artifact-obedient wrongness - the model faithfully
   executing a WRONG ruling you authored. Consistent failure across reps
   means the artifact is wrong, not the model.

   AUDIT THE EXPECTATION BEFORE FIXING THE SYSTEM. A sizeable share of red
   eval cases are usually wrong TESTS, not wrong behaviour - most often
   out-of-scope questions filed against the lens that should answer them
   instead of the one that should decline. A case asserting behaviour the
   governance is right to refuse is the case's bug; fix or park it
   (status: candidate) instead of patching the lens to satisfy it.

3. File the correction through the product, with an explicit target:

   dst correct <request_id> --kind definition --target "<term>" --note-file note.md

   ALWAYS pass target (the verb requires it). Without it, placement is
   vocabulary matching and a cross-cutting note lands on the wrong shared
   definition - where it becomes a blanket rule that regresses every other
   question using that term. A target naming a NEW term drafts a new
   definition. The note is a paragraph, not a flag - write it to a file and
   pass --note-file (or `-` to pipe it in); --note takes a one-liner. Add
   --corrected-sql when you know the SQL that would have been right.

4. Gate the draft - `dst patches draft <ticket_id>` drafts it and PRINTS
   the target and body; READ them before approving: the right target, and
   the right AMENDMENT. The drafter amends - rulings the correction never
   mentioned come back whether or not the model restated them - so what you
   are checking is whether the CHANGED ruling is right, not whether the rest
   survived. Approve with `dst patches approve <id> --dir .`; a bad
   draft gets `dst patches reject <id> --note "why"` - the note is fed
   to the next draft as a hard constraint, so say what to keep and what to
   drop.

   Approve writes into the file that already authors the term, whatever it
   is named. It defaults to this lens's tree; add `--shared` when the term
   is cross-cutting and belongs in semantic/definitions/ - that reaches
   every lens selecting it, so re-measure them all (step 5).

5. `dst apply`, then re-measure the WHOLE set including questions that
   were passing - a shared-asset edit reaches every lens selecting it, and
   the regression you cause is always in the question you were not looking
   at. Re-measure sibling lenses too when the patch touched semantic/.
   The fast form: `dst evals gate <lens>` runs ONE lens's publish gate as a
   dry run (seconds, against a multi-minute apply) - run it from the project dir so
   it gates the CANDIDATE tree, unpublished shared-asset edits included
   (outside a project dir it gates the server's stored bundle and says so).
   `dst plan` names which lenses a shared edit made stale - gate each.

6. Certify each verified win (the dst-certify skill, "own verified
   answers" section) - it pins the question against wobble, serves far
   faster (no generation), and lifts uncertified neighbors via the assisted
   tier. Then the eval gate has a corpus and future applies are guarded.

7. Residual wobble on questions whose artifacts are RIGHT (a rep drops a
   filter, forgets a carry-forward) is not an authoring problem - it is
   what certification exists for. Stop writing prose at that point.

Recovery: the lens project is a git repo. A bad patch is `git checkout` of
the file + `dst apply` - under a minute, no server surgery.
"""


def _warehouse_review_skill() -> str:
    """The scaffolded answerability review (the skill razor: the checks are
    computable from introspect/probe output the driver's agent already holds;
    the judgment about remedies is agent-side). Run after connect, before
    dst-semantic."""
    return """---
name: dst-warehouse-review
description: Use when asked to review a warehouse for answerability BEFORE
  building lenses on it - runs 12 shape checks over dst introspect/probe
  output and reports per-table findings, each with a rail / definition /
  remodel remedy. The failures these prevent are the ones no runtime check
  can catch.
---

# Review a warehouse for answerability

Most wrong answers that survive every verification check trace to a
TABLE-SHAPE decision, not to dst: the SQL is sound, the numbers ground, and
only the meaning is wrong. This review finds those shapes before a lens
exists. Input: `dst introspect --connection <name> --profile` output and
`profiles/<conn>.probe.json` (run `dst probe` first - value dictionaries,
row counts and partitioning are the evidence several checks read).

Report one finding per (table, shape): the shape, the evidence you computed,
the CONSEQUENCE the author would otherwise ship (say "any SUM over this
column is a multiple of the true value", not "this column is semi-additive"),
and the remedy TIER:

- rail - enforced in code, holds across phrasings (population_filter,
  pinned_dimensions, metric filters)
- definition - decisive prose plus `sql:`, which travels into generated SQL
- remodel - the shape should not reach the semantic layer at all

PROSE WILL NOT SAVE YOU: a rule in a description is obeyed for one phrasing
and violated for the next (measured three separate times). Recommending
"document this" for a shape that needs a rail is worse than no review.

## The 12 checks

1. Daily-snapshot fact table. Signal: a date column where
   distinct(PK) x distinct(dates) ~ row count. Consequence: totals grow with
   history; "now" without a MAX(date) pin reads a stale or empty day.
   Remedy: describe in `grain`; every current-state metric carries a
   `filters: [col = (SELECT MAX(col) ...)]`; consider a `_current` view.
2. Semi-additive column. Signal: a numeric column constant per
   (entity, period) across many rows. Consequence: any SUM is a multiple of
   the truth. Remedy: pinned_dimensions at minimum; PREFER remodel to one
   row per (entity, period) - the rail is coarse (it cannot check a range
   filter), the remodel removes the class.
3. Discriminator column. Signal: a low-cardinality column whose removal
   makes the PK non-unique. Consequence: multi-counting by exact multiples.
   Remedy: mandatory metric `filters` pinning one value.
4. Current-state column on a historical table. Signal: constant per entity
   across ALL dates while siblings vary. Consequence: flat or nonsense
   trends nobody questions. Remedy: never expose as a trend metric; say it
   is not point-in-time; remodel to the dimension table.
5. Dense / zero-filled grid. Signal: complete date coverage per entity AND a
   high share of zeros. Consequence: zero-vs-missing confusion both ways.
   Remedy: declare which convention holds in `grain`.
6. Scope-narrowed column name. Signal: a product/segment token its siblings
   lack (*_pms, *_emea). Consequence: a subset narrated as the company total
   (measured ~40% off, every check passing). Remedy: rename, or a decisive
   definition naming the trap in negative form + `not_computable` for the
   bare question.
7. Sentinel-dominated dimension. Signal: one literal ('(not set)', '',
   'unknown') over a large share of the value dictionary. Consequence:
   meaningless buckets; the dimension is unusable. Remedy: the dictionary
   already arms value_guard - flag it, and clean upstream.
8. Out-of-range dates. Signal: max(date) beyond today, or min far before
   the business start. Consequence: inflated unbounded totals. Remedy:
   `population_filter` bounding the column.
9. Multi-currency money. Signal: a currency-like column beside money
   columns with >1 distinct value. Consequence: a sum denominated in
   nothing. Remedy: `pinned_dimensions: [currency]`.
10. Duplicate measure across tables. Signal: same column name/semantics in
    2+ tables. Consequence: a bound declared on one is bypassed by phrasing
    that routes to the other. Remedy: declare it on EVERY exposing entity,
    or keep one canonical carrier.
11. Constant boolean. Signal: exactly one distinct value across all rows.
    Consequence: the column can answer nothing and invites a confident
    zero/total. Remedy: exclude it, or declare the question not_computable.
    (One line to check; caused a real wrong answer nothing caught.)
12. Placeholder metric. Signal: an entity metric with no `agg` and no
    `expr`. Consequence: generation improvises the ratio and gets the grain
    wrong. Remedy: fill it in or delete it. (Also one line.)

## Interactions worth flagging in the same pass

If a `current_x` metric pins MAX(date), do NOT also define `total_x` over
the SAME expression with an incompatible mandatory filter: the guard
resolves such twins by the question's wording, and a question naming
neither is rejected with the conflict spelled out. Give the second metric
its own expression or its own entity. (`dst apply` warns about the pair;
the review should catch it before the model exists.)

## Escalate rulings; never silently pick

Some findings are DECISIONS, not detections: period conventions ("last
week" = previous calendar week or trailing 7 days?), contested terms, which
of two defensible churn definitions is canonical. List them as decisions
owed by a human, with the candidate readings and how far apart the numbers
land. A ~1% error from an undeclared week convention fails no check ever -
this list is the only place it can be caught.

## Worked shape (the hostile-table pattern)

A daily grid carrying a monthly quota copied onto every row, dense-filled
per on-roster rep, stacks THREE shapes (snapshot grain + semi-additive +
dense fill). The entity's own description said "never SUM across days";
generation summed across days anyway, twice, two wrong magnitudes. The
finding to write: "remodel to one row per rep-month - no rail fully covers
this stack, and the description demonstrably does not."

Finish by proposing the semantic/ changes for every rail- and
definition-tier finding (the dst-semantic and dst-context skills hold the
authoring rules) and a remodel list for the data team, ordered by the cost
of the wrong answer each shape ships.
"""


def _history_bootstrap_skill() -> str:
    """The scaffolded history-bootstrap skill: mine warehouse query history into
    a draft semantic layer with the driver's agent. Judgment stays agent-side;
    dst's plan/apply gates are the safety net."""
    return """---
name: dst-history-bootstrap
description: Use when starting a dst project on a warehouse that already has
  query traffic - mines 30 days of query history into a draft semantic layer
  (entities, definitions incl. ambiguous terms where practice disagrees) and
  certified-answer candidates, then applies and verifies.
---

# Bootstrap the semantic layer from query history

The warehouse's query log is a usage-weighted map of the org's real semantic
layer: which tables carry the business, what the metrics are called, and where
practice already disagrees with itself. You (the agent) do the reading and the
judgment; dst's apply gates catch what you get wrong.

1. Pull shape-level history (metadata only - no table access needed). Save the
   result OUTSIDE the repo (query text can embed literals from your tables):

   BigQuery (needs bigquery.jobs.listAll; swap the region):

       SELECT query_info.query_hashes.normalized_literals AS shape_hash,
              ANY_VALUE(query) AS representative_text,
              COUNT(*) AS run_count,
              COUNT(DISTINCT user_email) AS principals
       FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
       WHERE job_type = 'QUERY' AND statement_type = 'SELECT'
         AND state = 'DONE' AND error_result IS NULL
         AND creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
         AND query_info.query_hashes.normalized_literals IS NOT NULL
         AND NOT STARTS_WITH(query, '/* dst:')
         AND NOT CONTAINS_SUBSTR(query, 'INFORMATION_SCHEMA')
       GROUP BY shape_hash
       HAVING COUNT(*) >= 2 OR COUNT(DISTINCT user_email) >= 2
       ORDER BY run_count DESC

   Snowflake (needs IMPORTED PRIVILEGES on the SNOWFLAKE database):

       SELECT query_parameterized_hash AS shape_hash,
              ANY_VALUE(query_text) AS representative_text,
              COUNT(*) AS run_count,
              COUNT(DISTINCT user_name) AS principals
       FROM snowflake.account_usage.query_history
       WHERE query_type = 'SELECT' AND execution_status = 'SUCCESS'
         AND start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP())
         AND query_text NOT LIKE '/* dst:%'
         AND query_text NOT ILIKE '%account_usage%'
       GROUP BY query_parameterized_hash
       HAVING COUNT(*) >= 2 OR COUNT(DISTINCT user_name) >= 2
       ORDER BY run_count DESC

2. Separate AUTHORED from GENERATED. Only authored SQL expresses judgment;
   generated SQL is a tool consuming metrics, not defining them. The tells:
   - dbt: leading comment {"app": "dbt", ...} - ingest via `dst import dbt`
     instead of mining.
   - BI pivot engines: __mask / rowDepth / colDepth projections, agg0_ aliases.
   - Semantic-layer compilers: __with_t_0-style generated CTEs.
   - dst itself: /* dst: ... */ (already filtered above).
   - Service-account principals running one shape on a schedule.
   Treat each generated family as ONE consumer surface however many filter
   permutations it ran.

3. Read the authored head. Group statements by metric intent - what is being
   measured (aggregate + columns) and what the author CALLED it (aliases are
   the org's own vocabulary). Usage weights tell you which tables and metrics
   carry the org; that is your entity shortlist.

4. Draft `semantic/`:
   - Entities for the load-bearing tables: grain from observed keys and joins,
     use_cases from what the queries actually do, fields the queries touch.
   - Definitions for recurring metrics, in the org's own vocabulary.
   - Where authored practice genuinely DISAGREES - same metric, different
     filters/grain/measure (e.g. turns counted per message vs per conversation)
     - write `status: ambiguous` with `possible_mappings` taken from the real
     variants. Ask, don't crown: run_count is popularity, not correctness.

5. Propose certified candidates from the most-run authored questions whose
   intent is unambiguous, into `certified_answers.yaml` with
   `source: "history:<shape_hash>"`. Leave `verified_by` for a human - never
   mark verified yourself; a certified answer is served VERBATIM, so a human
   must vouch before it counts.
   A shape cluster whose runs differ ONLY in benign literals (the period, a
   segment value) is ONE candidate, not N: author a single TEMPLATE - {slot}
   placeholders + `slots` types + `sample_bindings` (first = the most-run
   literals; see the dst-certify skill for the shape). But heed the
   gotcha below first: a literal difference that changes MEANING (a 120- vs
   180-day window definition) is a war to surface, never a slot to widen.

6. Select the drafted assets into a lens, then:

       dst plan
       dst apply --probe-certified
       dst query <lens> "<one real question per drafted metric>"

Gotchas:
- Literal-only differences can BE the war: a 120- vs 180-day window differs
  only in a literal the shape hash folded. Read the literals in the variants
  before declaring two statements equivalent.
- Scheduled traffic inflates run counts; weigh multi-principal shapes higher.
- The history export never goes into git.
"""


def _agents_md(name: str, api_port: int) -> str:
    return f"""# {name} - dst project (guide for AI agents)

This repo IS the source of truth for governed data access. Edit files, then
plan/apply; the server (and the dashboard, when bundled) renders this state -
never author in a UI.

## Layout
- `dst.yaml` - providers (LLM endpoints, BYOK), connection declarations.
  Every available field is listed at the bottom, commented, with defaults.
- `semantic/` - the SHARED semantic layer, edited in one place:
  `entities/<name>.yaml` (grain, fields, dimensions, metrics incl. filters,
  FK-side joins with relationship) and `definitions/<term>.md` (governed
  terms; `status: ambiguous` + possible_mappings makes dst ASK instead
  of guessing). Full field references in `semantic/README.md`.
- `lenses/<name>/` - one governed lens per dir: selection + policy + extras:
  - `lens.yaml` (`select:` over shared assets; model, access allow-list,
    rate limits - full commented reference at the bottom), `queries.yaml`
    (use_when + sample_queries), `definitions/*.md` (lens-LOCAL terms only;
    a term defined both shared and locally is an apply error),
    `certified_answers.yaml` (approved question->SQL), `evals/cases.yaml`.
    `compiled.yaml` is a server-rendered artifact - read it, never edit it.
- `.env` (gitignored) - secrets only; files refer to them by env name
  (`DST_API_KEY_<NAME>`). Never write a secret into a tracked file.
- `.claude/skills/dst-semantic/` - the warehouse-to-semantic-layer
  authoring loop as a Claude Code skill (introspect -> author -> select ->
  apply -> verify); other agents: read it as a plain procedure.
- `.claude/skills/dst-certify/` - importing verified BI queries (Looker/
  Metabase/Tableau exports, plain SQL) as certified answers with provenance.
- `.claude/skills/dst-history-bootstrap/` - mining warehouse query history
  into a draft semantic layer + certified candidates (metadata-only SQL,
  generated-vs-authored tells, ambiguity from real disagreement).
- `.claude/skills/dst-context/` - the authoring rules for
  definitions/instructions/context prose (decide, don't describe).
- `.claude/skills/dst-flywheel/` - wrong answer -> diagnose -> correct ->
  gate the patch -> re-measure -> certify; the incident loop.
- `.claude/skills/dst-warehouse-review/` - review a warehouse for
  answerability BEFORE building lenses: 12 shape checks over introspect/
  probe output, each with a rail / definition / remodel remedy.

## Workflow
```bash
dst dev                      # DB up + migrate + serve (API :{api_port}; + dashboard
                             # if this install bundles one - see startup output)
dst bootstrap --org <name>   # once: mints + saves DST_ADMIN_TOKEN to .env
dst plan                     # dry-run diff, files vs server
dst apply                    # files win; all-or-nothing: any error deploys NOTHING
dst query <lens> "..."       # ask a governed question from the terminal
```
FIND THE SERVER BEFORE STARTING ONE. Every verb talks to `DST_URL` - the
process env first, then `.env` (this project wrote
`DST_URL=http://localhost:{api_port}` there). A deployment or a session
already running sets it to something else, and that value is the truth: never
assume a port, never curl one to look. `dst dev` is only for when nothing
is serving yet.
Ask questions via MCP (`/mcp`, bearer = a caller key) or
`POST /v1/lenses/<lens>/query` with body `{{"q": "..."}}`. A caller only sees
lenses whose `access.allow` grants it - entries are objects, e.g.
`allow: [{{caller: alex}}]` or `[{{group: everyone}}]` (any valid key in the org).
Callers are PEOPLE (or service identities) - one key each, never shared, never
named after the tool asking on their behalf: that is what keeps every answer
attributable to a person.

## Rules for agents
- The UI never authors - files do. Governance (rulings, certify, revoke)
  runs from the CLI - `dst reviews`, `dst rule`, `dst correct`,
  `dst patches`, `dst revoke-key` - and the dashboard renders the same state
  when this install bundles one (a locally-built wheel is API-only; see
  `dst dev`'s startup line). Anything authored lives in this repo.
- Deletion of server OBJECTS is explicit: `dst lens rm` /
  `dst semantic rm`. File absence never deletes lenses, semantic assets,
  or connections; `dst plan` flags server-only objects to adopt
  (`dst export --lens <name>`) or leave for their owner. The exception
  is file-managed certified ANSWERS: removing an entry from
  certified_answers.yaml deletes it on apply (files win; review-promoted
  answers survive).
- Change governed meaning by editing `semantic/` (shared) or the lens's local
  `definitions/*.md`, then plan -> apply. `dst plan` names every lens a
  shared edit makes stale; apply recompiles them. Never claim a change is
  live until apply succeeded.
- Uncomment fields from the reference blocks instead of guessing names.
- Secrets: add the env name to `.env`, reference it as `secret_env`/`api_key_env`.
- Scaffolding: `dst introspect --connection X --profile` prints the schema
  PLUS profile facts (row counts, null rates, enum values, ranges) - read it,
  then author semantic/ files yourself. Without --profile it is schema only and
  says NOT PROFILED at the top; `--json` prints the same facts parseable.
  `dst import dbt --target-dir target/ --connection X` scaffolds from dbt
  artifacts one-shot.
- Need actual ROWS to settle what a column means? `dst sql "SELECT ..."
  --connection X --limit 5` - SELECT-only, row-capped, logged. Never open the
  warehouse client directly: dst holds that credential so you do not have
  to, and SQL run around it is ungoverned and invisible to the audit log.
- Verifying access: `dst query <lens> "<q>" --key dst_...` asks AS that
  caller. The admin token bypasses every allow-list, so it is the only way to
  prove a grant works - and that an ungranted caller is refused.

Reference documentation (every verb, every field): https://www.dataservetool.com
"""
