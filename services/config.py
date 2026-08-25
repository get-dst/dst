"""Application settings, loaded from environment / .env (pydantic-settings).

Every setting answers to ``DST_<NAME>``. The bare names they used to answer to
still work — one deprecation line, then business as usual — because a rename that
breaks a running deploy is a worse bug than the one it fixes.

The prefix is not tidiness. dst installs from PyPI onto machines whose shells
belong to somebody else, and a settings model with no prefix claims ``ENVIRONMENT``,
``SECRET_KEY``, ``PROVIDERS``, ``EDITION``, ``PROJECT_FILE`` — names half the
ecosystem already uses. Not a hypothetical: an ambient ``ENVIRONMENT=production``
in somebody's shell reaches ``_production_dsn_ssl`` at import, rewrites both DSNs,
and kills the whole process at collection time, from a variable nobody set for dst.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, AliasGenerator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from services.contracts.authoring import Authored

log = logging.getLogger("dst")


def _dst_alias(field_name: str) -> AliasChoices:
    """``DST_<NAME>`` first, the bare ``<NAME>`` second — so a prefixed value
    always wins over a legacy one, in the process env and in a .env alike."""
    bare = field_name.upper()
    return AliasChoices(f"DST_{bare}", bare)


# The bare names that are NOT deprecated, reasoned per name rather than by blanket.
# Each is somebody else's convention that dst answers to on purpose:
#
#  DATABASE_URL         every PaaS injects it by that name (Heroku, Render, Railway,
#                       Fly) — a tool that ignores it is the one that is wrong.
#  DATABASE_ADMIN_URL   its documented pair in every deploy artifact we ship. Teaching
#                       `DATABASE_URL` + `DST_DATABASE_ADMIN_URL` side by side reads
#                       as a typo, and no platform injects the admin half anyway.
#  CLERK_*              Clerk's own names; its hosting integrations set them for you.
#
# Everything else is dst's own coinage (GCP_CREDENTIALS is ours: Google's are
# GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_CLOUD_PROJECT), so it moves under the prefix.
_FIRST_CLASS_BARE = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_ADMIN_URL",
        "CLERK_SECRET_KEY",
        "CLERK_PUBLISHABLE_KEY",
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
    }
)


class ProviderConfig(Authored):
    """One configured LLM provider: a name (its key in the providers map), a wire
    type, a secret, and optionally its models. No vendor is special-cased in the
    codebase — the core knows wire shapes; your config knows vendors.
    """

    type: Literal["anthropic", "openai-compatible", "local"] = Field(
        description="wire protocol: 'anthropic', 'openai-compatible' (covers OpenAI, "
        "DeepSeek, Ollama, vLLM, Groq, most gateways), "
        "or 'local' (in-process embeddings, no key — `dst-core[local-embed]` extra)"
    )
    api_key: str | None = Field(
        default=None, description="inline secret — only for env-var config, never in files"
    )
    api_key_env: str | None = Field(
        default=None,
        description="name of the process-env var holding the key (convention: "
        "DST_API_KEY_<NAME>); the right choice in dst.yaml",
    )
    base_url: str | None = Field(
        default=None, description="API base URL — required for openai-compatible"
    )
    fast_model: str | None = Field(
        default=None,
        description="this provider's cheap first-pass model; the first declared "
        "provider with one carries the org's fast tier",
    )
    smart_model: str | None = Field(
        default=None, description="this provider's quality model (smart tier)"
    )
    models: list[str] = Field(
        default_factory=list,
        description="extra model names this provider serves, for bare-ref resolution",
    )
    embedding_model: str | None = Field(
        default=None,
        description="embedding model this provider serves "
        "(openai-compatible uses POST {base_url}/embeddings)",
    )
    embedding_dim: int | None = Field(
        default=None,
        description="embedding vector dimension (default 1024). Changing it on an "
        "install that already holds vectors requires `dst reindex` — the "
        "write-path guard blocks mismatched writes until then",
    )
    reasoning: bool | None = Field(
        default=None,
        description="do this provider's models bill their thinking against max_tokens? "
        "Reasoning models spend it BEFORE any answer lands, so a cap tuned for a "
        "normal model returns an empty reply; set true and the provider adds thinking "
        "headroom to every call (a cap is a ceiling, not a spend — it costs nothing "
        "when output is short). Leave unset to decide per model name",
    )

    @model_validator(mode="after")
    def _shape_checks(self) -> ProviderConfig:
        if self.type == "openai-compatible" and not self.base_url:
            raise ValueError("openai-compatible provider entries require base_url")
        if self.embedding_dim is not None and self.embedding_dim < 1:
            raise ValueError("embedding_dim must be a positive integer")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Every field reads DST_<NAME>, then its legacy bare <NAME>. Generated
        # rather than spelled out per field, so a field added tomorrow is prefixed
        # tomorrow — including for tests/conftest.py, which derives the names it
        # must scrub from `model_fields[…].validation_alias`.
        alias_generator=AliasGenerator(validation_alias=_dst_alias),
        # …and the field name still works as a keyword: `Settings(environment="x")`.
        populate_by_name=True,
    )

    # Core
    environment: str = "local"
    log_level: str = "INFO"
    # Which edition this install runs ("oss" | "cloud"). UI badging only — core
    # behavior must never gate on it (no feature flags in core).
    edition: str = "oss"
    # When set, the built SPA at this dir is served (same-origin) with API + SPA fallback.
    web_dist: str | None = None
    # Public base URL (e.g. https://dst.example.com) for building review tracking links.
    public_base_url: str | None = None
    # Extra CORS origins (comma-separated) for split frontend/backend deploys.
    cors_origins: str | None = None

    # Datastore.
    # The app connects as a NON-superuser role so RLS is enforced (superusers bypass RLS).
    database_url: str = "postgresql+psycopg://dst_app:dst_app_dev@localhost:5432/dst"
    # The admin URL (superuser) runs migrations + bootstrap (role/org creation, seeding).
    database_admin_url: str = "postgresql+psycopg://dst:dst_dev@localhost:5432/dst"
    # Pool sizing, per engine per process. Managed Postgres counts every
    # connection against a small ceiling (RDS db.t3.micro ≈ 85): budget
    # instances × 2 engines × (pool_size + max_overflow) below it.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Recycle pooled connections older than this (seconds): managed proxies
    # (RDS Proxy, Cloud SQL) idle-kill silently; pre_ping alone just eats the RTT.
    db_pool_recycle: int = 1800

    # Bound on each per-request model/embedding call on the serving path
    # (services/runtime/bounded.py). An embedder cold start or a wedged provider
    # socket can hang for as long as the process lives, with nothing logged; the
    # default is deliberately generous — several times a slow multi-step
    # generation — so a real answer is never cut off. 0 disables the bound.
    serving_timeout_s: float = 600

    # Wall-clock budget for the certify self-test inside `dst apply`
    # (services/project/apply.py::_certify_self_test). That self-test runs a full
    # generation per answer the push landed, while apply holds the org's apply
    # advisory lock AND its Postgres transaction open for the whole request — so
    # an unbounded push of several answers can run for minutes with the org
    # wedged behind it and outlast `dst apply --timeout` (300s): the CLI reports a
    # hang while the server is still working. Bounded here: answers past the
    # budget land UNTESTED, loudly, and `dst test` sweeps them unbounded
    # (no lock, no transaction). 0 disables the bound.
    #
    # READ IT IN ANSWERS, NOT SECONDS. One case is one full generation, so this
    # budget covers (this value / your lens's generation latency) answers of a
    # push and no more; "120 seconds" tells an author nothing about how much of
    # their push gets covered. Size it as (answers you want covered) x (this
    # lens's generation latency).
    certify_selftest_budget_s: float = 120

    # Local test warehouse (jaffle).
    duckdb_jaffle_path: str = "fixtures/jaffle_shop.duckdb"

    # BigQuery (real warehouse). `gcp_credentials` = the SA JSON path
    # (DST_GCP_CREDENTIALS; these are ours, not Google's — theirs are
    # GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_CLOUD_PROJECT).
    gcp_credentials: str | None = None
    gcp_project: str | None = None
    bigquery_dataset: str = "bigquery-public-data.thelook_ecommerce"
    # 10 GB, not 1 GB: a month-scoped ratio over an ordinary few-million-row
    # mart already scans several GB, and at 1 GB the refusal reads as "dst
    # cannot answer this" rather than "dst is configured not to". 10 GB is a
    # few cents per query on-demand — still a real cost guard, no longer a trap
    # for ordinary marts.
    bigquery_max_bytes_billed: int = 10_000_000_000
    # How many eval-gate cases score at once. The work is provider-bound HTTP
    # with no shared mutable state, and serial execution made a full apply
    # 15-30 minutes of back-to-back round-trips (a measured flat ~2.7s
    # inter-call gap with zero overlap). Conservative default; DST_GATE_CONCURRENCY
    # raises it per install, 1 restores strictly-serial.
    gate_concurrency: int = 4

    # LLM providers, JSON keyed by name (declaration order IS the tier/cost
    # preference — put the cheap provider first). BYOK: a provider is a name +
    # type + secret; no vendor-named key settings exist. Example:
    #  DST_PROVIDERS={"deepseek": {"type": "openai-compatible",
    #    "base_url": "https://api.deepseek.com", "api_key": "sk-…",
    #    "fast_model": "deepseek-v4-flash"}, "anthropic": {"type": "anthropic",
    #    "api_key_env": "MY_ANTHROPIC_KEY"}}
    providers: Annotated[dict[str, ProviderConfig], NoDecode] = Field(default_factory=dict)
    # Bare model refs fall back to this entry (else the first declared).
    default_provider: str | None = None
    # The background profiling chain's LLM description pass: table/column names and
    # sampled example values go to the configured provider to fill undocumented
    # columns. Set DST_LLM_DESCRIPTIONS=false to keep profiling entirely between
    # dst and the warehouse (docs: Security & data flow).
    llm_descriptions: bool = True
    # Entry-point plugins (services/plugins.py) allowed to mount routes, by name,
    # comma-separated. Unset = every installed one mounts (what dst-cloud needs;
    # they are logged and shown in /ready either way). Set = exactly these, and an
    # installed plugin outside the list is refused out loud — the route table becomes
    # a declared fact instead of a consequence of what is in the venv.
    plugins: str | None = Field(default=None, validation_alias=AliasChoices("DST_PLUGINS"))
    # The file-first workspace config; providers/pricing/connections declared there
    # fill any gaps the env leaves (env always wins). Relative to the server's CWD.
    project_file: str = "dst.yaml"
    # Per-model price overrides/additions, JSON: {"model": [usd_per_mtok_in, usd_per_mtok_out]}.
    # Models absent here and from the built-in table trace as "unpriced" (cost NULL).
    ai_pricing: Annotated[dict[str, tuple[float, float]], NoDecode] = Field(default_factory=dict)

    @field_validator("providers", "ai_pricing", mode="before")
    @classmethod
    def _decode_json_env(cls, v: object) -> object:
        # We own the decoding (NoDecode): compose/CI often pass VAR="" for
        # optional JSON vars — treat empty as unset instead of crashing at boot.
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    # Fernet key(s) for encrypting stored warehouse credentials (DST_SECRET_KEY).
    # COMMA-SEPARATED: the first encrypts, all are tried for decryption — that is what
    # makes `dst rotate-key` possible. Generate one with `dst secret`.
    secret_key: str | None = None

    # Caller-key expiry policy. Both default to "no policy", because every key issued
    # before this is non-expiring and silently expiring them would be a breaking
    # change delivered as an upgrade. An operator who sets the cap gets it enforced on
    # every mint; `_max` wins over `_default` and over an explicit per-key request.
    token_default_expiry_days: int | None = None
    token_max_expiry_days: int | None = None

    # Generic OIDC dashboard auth — the free-tier, self-host path that lets any
    # standard IdP (Keycloak, Authentik, Zitadel, Okta, Entra, Google) in. Sits
    # BESIDE Clerk, not replacing it: a self-hoster who runs their own IdP has a way
    # in without a hosted vendor. Enabled by setting the issuer.
    oidc_issuer: str | None = None
    # The token's expected audience (usually the OIDC client id). Verified — an
    # omitted audience against a multi-tenant IdP is the classic "any tenant's token
    # validates" hole, so if this is unset we still require it before trusting a token.
    oidc_audience: str | None = None
    # Override the JWKS URL; otherwise discovered from the issuer's
    # /.well-known/openid-configuration (Keycloak, Auth0, Okta, Entra all differ on the
    # path, so discovery is the portable default).
    oidc_jwks_url: str | None = None
    # The claim carrying the user's groups/roles, mapped to caller groups so lens
    # allow-lists can grant by group. Keycloak/Authentik use "groups"; some use "roles".
    oidc_groups_claim: str = "groups"
    # A group value that grants admin. Unset ⇒ OIDC users are non-admin by default
    # (safe): the operator decides which IdP group is privileged.
    oidc_admin_group: str | None = None
    # Display name for the org OIDC users share. All users of one issuer land in one
    # org (the self-host single-company case); per-claim multi-tenancy is a later add.
    oidc_org: str = "oidc"

    # Clerk dashboard auth (issuer is derived from the publishable key).
    clerk_secret_key: str | None = None
    clerk_publishable_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DST_CLERK_PUBLISHABLE_KEY",
            "CLERK_PUBLISHABLE_KEY",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
        ),
    )

    # Private-demo sandbox config does NOT live here: the sandbox module
    # (services/lenses/sandbox.py) is excluded from the public cut and reads its
    # own env surface, so the shipped Settings model carries no demo-only names.

    @model_validator(mode="after")
    def _production_dsn_ssl(self) -> Settings:
        # libpq's default sslmode=prefer silently downgrades to cleartext when the
        # server allows it — in production, opt DSNs into encryption unless they
        # already chose an sslmode or target a unix socket (SSL doesn't apply).
        if self.environment == "production":
            self.database_url = _require_sslmode(self.database_url)
            self.database_admin_url = _require_sslmode(self.database_admin_url)
        return self


def _require_sslmode(url: str) -> str:
    if "sslmode=" in url or "host=/" in url or "host=%2F" in url:
        return url
    return url + ("&" if "?" in url else "?") + "sslmode=require"


def legacy_env_names() -> dict[str, str]:
    """``{deprecated bare name: the DST_ name that replaced it}``.

    Derived from the field declarations, not listed by hand: the alias generator is
    the single source of the pairing, and ``_FIRST_CLASS_BARE`` is the single list of
    exceptions. A field added tomorrow needs no entry here."""
    out: dict[str, str] = {}
    for info in Settings.model_fields.values():
        alias = info.validation_alias
        if not isinstance(alias, AliasChoices):
            continue
        prefixed, *legacy = (str(choice) for choice in alias.choices)
        if not prefixed.startswith("DST_"):
            continue
        out.update({old: prefixed for old in legacy if old not in _FIRST_CLASS_BARE})
    return out


def warn_legacy_env_names() -> None:
    """One line, once per process, naming every bare name still steering this install
    and what to rename it to. Silence when the prefixed name is also set — that one
    wins, so nothing is being steered by the legacy spelling.

    Scoped to the process environment on purpose. The ambient shell is the vector
    this defect is about (a stranger's ``ENVIRONMENT``/``SECRET_KEY``), and it is the
    one input that is identical on every machine once tests/conftest.py scrubs it —
    reading ./.env here would make the suite's own output depend on whether the
    checkout happens to have one, which is the disease, not the cure."""
    stale = {
        old: new
        for old, new in legacy_env_names().items()
        if old in os.environ and new not in os.environ
    }
    if stale:
        log.warning(
            "deprecated environment variable(s) %s — dst's settings are "
            "DST_-prefixed now; the bare names are read for compatibility and will "
            "stop being read in a future release",
            ", ".join(f"{old} (use {new})" for old, new in sorted(stale.items())),
        )


settings = Settings()
warn_legacy_env_names()

# Where this process looks for a project's `.env`. The server's own directory,
# until a CLI verb adopts a `--dir` project (see `adopt_project_env`).
_project_dirs: tuple[str | Path, ...] = (".",)


def adopt_project_env(dirs: Sequence[str | Path]) -> Callable[[], None]:
    """Point this process's configuration at the project(s) under *dirs*, and return
    the undo.

    This is the in-process half of ``dst <verb> --dir X``: the settings singleton
    is a module global, so ``dst test --dir X`` run from anywhere else would
    otherwise sweep whatever database the SHELL's project uses and report it as X's.

    It used to work by copying every line of ``<dir>/.env`` into ``os.environ`` with
    ``setdefault`` and never unwinding it. In a one-shot CLI that is invisible; as a
    library call, or in any process that outlives the verb, it is state pollution —
    every later caller in that process inherits the first project's credentials, for
    the rest of the session, with nothing saying where they came from. And
    os.environ is not ours: a project's ``.env`` could reconfigure any library in the
    process that happens to read the same name.

    So nothing is written to the environment. The two things that actually consume a
    project's ``.env`` get it directly:

      * ``Settings`` — rebuilt IN PLACE from ``<dir>/.env`` (every module holds a
        reference to this one object), with the same precedence as before and as the
        HTTP verbs: process env wins, then ``<dir>/.env``, and never the cwd's (see
        ``services/cli/main.py::_env_dirs`` — an explicit --dir must not borrow a
        neighbouring project's credentials);
      * ``resolve_env_ref`` — the declared refs (a provider's ``api_key_env``, a
        connection's ``secret_env``), whose default search path becomes *dirs*.

    Both are dst's own state, and both are restored by the returned callable, so
    an adopted project lasts exactly as long as the invocation that asked for it."""
    global _project_dirs
    before_dirs, before_settings = _project_dirs, dict(settings.__dict__)
    _project_dirs = tuple(dirs)
    envfiles = [str(Path(d) / ".env") for d in dirs]
    # `_env_file` is BaseSettings' own runtime kwarg; pydantic's dataclass_transform
    # makes mypy synthesise __init__ from the FIELDS alone, so it cannot see it.
    settings.__dict__.update(Settings(_env_file=envfiles).__dict__)  # type: ignore[call-arg]

    def undo() -> None:
        global _project_dirs
        _project_dirs = before_dirs
        settings.__dict__.update(before_settings)

    return undo


def validate_production_contract() -> None:
    """Fail server startup loudly when the production env contract is unmet.

    Called at `services.app` import time, so `dst serve` and the container
    entrypoint both hit it, while CLI verbs (migrate/bootstrap) stay usable
    with only their own requirements. Contract: an unset
    DST_SECRET_KEY silently degrades to a per-process OAuth signing secret
    (breaks multi-instance), an unset DST_PUBLIC_BASE_URL trusts forwarded
    Host headers in OAuth metadata, and the historical dev DB password is a
    public credential."""
    if settings.environment != "production":
        return
    missing = [
        name
        for name, value in (
            ("DST_SECRET_KEY", settings.secret_key),
            ("DST_PUBLIC_BASE_URL", settings.public_base_url),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"production deployment contract unmet — set {', '.join(missing)} "
            "(see the configuration reference at https://www.dataservetool.com)"
        )
    # Local import: this module must not need sqlalchemy at import time.
    from sqlalchemy.engine import make_url

    if make_url(settings.database_url).password == "dst_app_dev":
        raise RuntimeError(
            "DATABASE_URL carries the well-known dev password 'dst_app_dev' — every "
            "pre-release install shipped it, so it authenticates anyone. Set a real "
            "password (compose: DST_APP_DB_PASSWORD) and run `dst migrate`, which "
            "applies DATABASE_URL's password to the app role."
        )


class EnvRefError(ValueError):
    """An ``@/path`` env ref that names a file which cannot be read.

    Typed so callers can turn it into their own surface's configuration error
    (apply reports it per-connection) instead of matching on message text."""


def resolve_env_ref(
    env_name: str | None, *, dirs: Sequence[str | Path] | None = None
) -> str | None:
    """Resolve a declared env ref (api_key_env / secret_env): process env first,
    else the first `.env` under ``dirs`` that defines it (default: this process's
    project directory — ``.``, the server's own, unless a CLI verb adopted one with
    ``adopt_project_env``; the CLI's --dir verbs pass the named project and nothing
    else, so an out-of-tree invocation authenticates as that project and can never
    borrow the shell's) — read live, because pydantic loads only its own fields
    from .env and --reload doesn't restart on .env edits. Never from tracked
    files.

    A value of ``@/path/to/file`` resolves to that file's contents — for secrets
    that already live on disk as files (a BigQuery service-account JSON), so the
    .env line points at the file instead of inlining a blob."""
    if not env_name:
        return None
    val = os.environ.get(env_name)
    if not val:
        for d in _project_dirs if dirs is None else dirs:
            envfile = Path(d) / ".env"
            if not envfile.exists():
                continue
            for line in envfile.read_text(encoding="utf-8").splitlines():
                if line.startswith(env_name + "="):
                    val = line.split("=", 1)[1].strip() or None
                    # Shell habits wrap values in quotes; keeping them would feed
                    # the quote characters into the secret.
                    if val and len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                        val = val[1:-1] or None
                    break
            if val:
                break
    if val and val.startswith("@"):
        ref = Path(val[1:]).expanduser()
        try:
            return ref.read_text(encoding="utf-8").strip()
        except OSError as exc:
            # A typo'd path used to degrade to None — indistinguishable from an
            # unset secret while the user stares at the line that sets it
            # . An @-ref is a stated intention: fail loud,
            # naming the env var and the absolute path tried.
            raise EnvRefError(
                f"{env_name}: @-ref {ref.expanduser().resolve()} could not be read "
                f"({exc.strerror or exc}) — a leading @ must name a readable file"
            ) from exc
    return val
