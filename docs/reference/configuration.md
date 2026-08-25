# Configuration

Configuration lives in three places, by design:

- **`dst.yaml`**: the committed project file (providers, pricing, connection
  declarations). Never holds a secret: an inline `api_key` is a parse error, not a lint
  warning (`services/project/schema.py`).
- **`.env`**: the gitignored secrets file. Tracked files refer to its entries by
  env-var name only.
- **Process environment**: server settings (pydantic-settings, `services/config.py`).
  Providers, pricing, and connections declared in `dst.yaml` fill any gaps the env
  leaves; the env always wins.

Env refs (`api_key_env` / `secret_env`) resolve in order: process env, then a *live*
read of `./.env` (so `--reload` picks up edits without a restart). A value of
`@/path/to/file` loads that file's contents: the idiom for a BigQuery service-account
JSON (`services/config.py`).

## dst.yaml

Top-level keys (`services/project/schema.py`):

| Key | What |
|---|---|
| `name` | project name |
| `providers` | LLM providers, keyed by name; see fields below |
| `default_provider` | which entry bare model refs fall back to (else the first declared) |
| `ai_pricing` | per-model price overrides, `{"model": [usd_per_mtok_in, usd_per_mtok_out]}`; a model absent here and from the built-in table traces as unpriced, and its whole trace's cost is `NULL`, never a default |
| `connections` | warehouse declarations, keyed by name |

### Provider fields (`providers.<name>`)

No vendor is special-cased: the core knows wire shapes, your config knows vendors
(`services/config.py`). Declaration order is the tier/cost preference: put the cheap
provider first.

| Field | What |
|---|---|
| `type` | wire protocol: `anthropic`, `openai-compatible` (covers OpenAI, DeepSeek, Ollama, vLLM, Groq, most gateways), `voyage` (embeddings-only), or `local` (in-process embeddings, no key; the `dst-core[local-embed]` extra) |
| `api_key_env` | name of the env var holding the key (convention: `DST_API_KEY_<NAME>`); the only right choice in a committed file |
| `base_url` | API base URL; required for `openai-compatible` |
| `fast_model` | this provider's cheap first-pass model; the first declared provider with one carries the fast tier |
| `smart_model` | this provider's quality model |
| `models` | extra model names this provider serves, for bare-ref resolution |
| `embedding_model` | embedding model this provider serves (`voyage` defaults to `voyage-3.5`; `openai-compatible` uses `POST {base_url}/embeddings`) |
| `embedding_dim` | embedding vector dimension (default 1024). Changing it on an install that already holds vectors requires `dst reindex`; the write-path guard blocks mismatched writes until then |
| `reasoning` | do this provider's models bill their thinking against `max_tokens`? Leave unset and dst decides per model name; set `true`/`false` to override; see below |

#### The three embedding postures

Embeddings power certified-answer matching and routing, and the scaffold spells
out three postures (`services/cli/init.py`):

- **poc**, `{type: local}`: in-process, no API key
  (`pip install 'dst-core[local-embed]'`); matching bands auto-adjust.
- **production**: an `openai-compatible` entry with `embedding_model` +
  `base_url`, or a `voyage` entry.
- **none**: omit it entirely. Generation, guards and evals still work, but
  certified matching cannot run and routing falls back to lexical. Every answer
  then carries a degraded line saying certified matching could not run:
  degraded visibly, never silently wrong.

#### Reasoning models and token caps

A reasoning-mode model spends its thinking against the same `max_tokens` budget as
the answer, and it spends it **first**. A cap sized for a normal model can
therefore come back **empty** (zero characters of SQL), and callers then
degrade silently through their own fallbacks.

dst handles this in the provider layer, so call sites keep asking for the
*answer* size they want and thinking headroom is added on the wire. Detection is
config-first: `reasoning` is authoritative when you set it, and when you leave it
unset the model name decides (the DeepSeek reasoning line, `-r1` models, the Claude
models that think by default, and names containing `reasoner`/`reasoning`/`thinking`).

**Set `reasoning: true` when your provider serves a reasoning model whose name does
not say so.** It is free to be wrong in that direction: a cap is a ceiling, not a
spend. You are billed for tokens produced, not tokens allowed. A provider whose
models don't reason is completely unaffected either way.

```yaml
providers:
  house:
    type: openai-compatible
    base_url: https://llm.internal
    api_key_env: DST_API_KEY_HOUSE
    smart_model: house-thinker-v2   # name gives no hint, so say it explicitly
    reasoning: true
```

If an LLM-backed step degrades for no visible reason, check the logs for
`empty completion from …`: that warning names the model and the cap that
produced nothing.

### Connection fields (`connections.<name>`)

| Field | What |
|---|---|
| `type` | warehouse: `duckdb` \| `postgres` \| `mysql` \| `bigquery` \| `snowflake` |
| `config` | non-secret connector settings (host, project, path, …). BigQuery's `project` and Snowflake's `database` also become the connection's **default catalog**: an entity's `source.table` written `schema.table` compiles to `<catalog>.schema.table`, so the same entity file serves two environments ([Environments and CI](../guides/environments-and-ci.md)) |
| `secret_env` | env var holding the credential (convention: `DST_API_KEY_<NAME>`); a secret value itself can never appear here |

#### What goes in `config`, per type

`config` is a free-form mapping, so an unrecognised key sits there doing nothing —
apply warns (`config_warnings`) rather than failing, and these are the keys each
connector actually reads (`services/lenses/connections.py`):

| Type | Keys |
|---|---|
| `bigquery` | `project`, `dataset`, `datasets`, `schema`, `max_bytes_billed` |
| `snowflake` | `account`, `user`, `warehouse`, `database`, `schema`, `role`, `auth`, `private_key_passphrase` |
| `postgres` | `host`, `port`, `database`, `user`, `schema`, `sslmode`, `statement_timeout_ms` |
| `mysql` | `host`, `port`, `database`, `user`, `statement_timeout_ms` |
| `duckdb` | `path`, `schema` |

**The cost and time caps belong here**, not in a shell: `max_bytes_billed` (BigQuery,
default 10 GB) and `statement_timeout_ms` are per-connection, so a wide-scanning lens
can be raised without loosening the cap on a connection that should never scan wide —
and the value is reviewed in the repo instead of living in one operator's environment.
`DST_BIGQUERY_MAX_BYTES_BILLED` still overrides globally.

Example:

```yaml
name: analytics

providers:
  anthropic:
    type: anthropic
    api_key_env: DST_API_KEY_ANTHROPIC
  cheap:
    type: openai-compatible
    base_url: https://api.deepseek.com
    api_key_env: DST_API_KEY_CHEAP
    fast_model: their-fast-model

connections:
  wh:
    type: snowflake
    config: {account: my-account, warehouse: compute_wh}
    secret_env: DST_API_KEY_WH
```

!!! warning "Snowflake: move off passwords"
    Snowflake is retiring single-factor password authentication, with final
    enforcement landing **August–October 2026**. Point `secret_env` at a **PEM
    private key** instead of a password and dst uses keypair auth, detected
    from the key's shape, or forced with `config: {auth: keypair}`. An
    encrypted key takes `config: {private_key_passphrase: …}`. The password path
    still works until Snowflake removes it.

Every YAML `dst init` scaffolds ends with a commented reference block rendered from
these schemas (`services/project/template.py`): a new config field can never silently
miss the docs. Uncomment fields there instead of guessing names.

## Entity rails (`semantic/entities/<name>.yaml`)

The deterministic behaviour levers on an entity, enforced by the compiler and the
serve-time checks, not by hoping the model reads prose (see the
[project-files guide](../guides/project-files.md#the-deterministic-rails-on-an-entity)
for the full mechanism table):

| Key | Type | What it enforces |
|---|---|---|
| `population` | string | Declares who/what the rows cover; the `population_declared` serve check requires answers to carry the scope |
| `population_filter` | SQL predicate | The compiler ANDs it into **every** query against the entity; scope bound no phrasing can smuggle past |
| `pinned_dimensions` | list of fields | Each must be pinned or GROUPed before any aggregate (`aggregation_scope` check); "never sum across currencies", structurally |
| `filters:` (on a metric) | SQL predicates | Part of the metric's meaning; the filter guard rejects the metric computed without them |

## Lens serve-time contracts (`lenses/<name>/lens.yaml`)

Two lens keys declare contracts the server enforces at serve time; the
scaffold's reference block carries the full key list
(`services/contracts/lens_config.py`):

| Key | Default | What it declares |
|---|---|---|
| `stale_after_days` (top level) | unset | the freshness contract: days after the scope's measured last-update at which answers flag stale: the freshness check fails, the grade caps at `partial`, and the answer says so. Unset = undeclared; the check reports skip, never a vacuous pass. Read [how `data_as_of` is measured](../guides/project-files.md#freshness-is-a-declared-contract) before setting it |
| `model.answer_contract` | `strict` | the output-grid contract in every generation prompt: project exactly the asked quantities at the asked grain, full precision. `off` removes the block (a bare `off` is fine; YAML 1.1 parses it as a boolean and the loader coerces it back) |

## Server environment

Loaded from the process env / `.env` (`services/config.py`). Every setting is
`DST_`-prefixed; see [Names and compatibility](#names-and-compatibility) for the
handful that deliberately are not, and for the unprefixed names still read from
older installs. The interesting ones:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local dev URL | the app's connection: a non-superuser role, so row-level security is enforced |
| `DATABASE_ADMIN_URL` | local dev URL | migrations + bootstrap (role/org creation) |
| `DST_SECRET_KEY` | — | Fernet key(s) encrypting stored warehouse/context credentials, **comma-separated**: the first encrypts, all decrypt. `dst secret` mints one; `dst init` generates it into `.env`. To rotate, deploy `<new>,<old>`, run `dst rotate-key`, then drop `<old>`. Changing it without that sequence makes stored credentials unreadable; the server checks the key against an encrypted sentinel at startup and refuses to boot on a mismatch |
| `DST_PROVIDERS` | `{}` | providers as JSON, same shape as the `dst.yaml` `providers` map; inline `api_key` is allowed *here* (env-only), never in files |
| `DST_DEFAULT_PROVIDER` | first declared | bare-model-ref fallback |
| `DST_LLM_DESCRIPTIONS` | `true` | the profiling chain's LLM description pass, which sends table/column names and sampled values to the configured provider to fill undocumented columns. `false` turns it off; profiling then stays entirely between dst and the warehouse. See [Security and data flow](../security.md) |
| `DST_AI_PRICING` | `{}` | per-model pricing JSON, same shape as the file key |
| `DST_PROJECT_FILE` | `dst.yaml` | the workspace config, relative to the server's cwd |
| `DST_PLUGINS` | unset | entry-point plugins allowed to mount routes, comma-separated by name. Unset means every installed one mounts; set means exactly these, and an installed plugin outside the list is refused with a warning naming it. Either way every mount is logged and listed in `/ready`; see [Plugins](#plugins) |
| `DST_AUDIT_INTERVAL_HOURS` | `24` | interval of the standing drift re-audit per connection; `0` disables the loop. For a one-shot bootstrap from query history, use the [history-bootstrap skill](../guides/drift-audit.md) instead |
| `DST_EVAL_INTERVAL_HOURS` | `24` | standing certified-suite re-run per published lens; `0` disables (use `dst test` + cron instead) |
| `DST_SERVING_TIMEOUT_S` | `600` | bound on each per-request model/embedding call while serving a query. Generous on purpose: the slowest legitimate generations run for minutes; it exists so a wedged provider (or an embedder cold start downloading model weights) surfaces as an error naming the stage instead of a request that never returns. `0` disables the bound |
| `DST_PUBLIC_BASE_URL` | — | public base URL for review tracking links and OAuth metadata behind a proxy |
| `DST_CORS_ORIGINS` | — | extra origins, comma-separated, for split frontend/backend deploys |
| `DST_TOKEN_DEFAULT_EXPIRY_DAYS` | — | expiry applied to newly issued `dst_` caller keys. Unset = the key never expires |
| `DST_TOKEN_MAX_EXPIRY_DAYS` | — | hard cap on caller-key lifetime. Applies to every mint, including one that explicitly requests longer; the request is **capped, not refused**, so an operator gets a usable key rather than none |
| `DST_WEB_DIST` | auto-detected | directory of the built dashboard SPA; `dst serve` finds the source build or the wheel's bundled copy on its own. When **neither** exists (any wheel built without the web step is API-only), startup says `dashboard: not bundled in this install` and every CLI verb still works; to add the UI: `pnpm -C apps/web install && pnpm -C apps/web build`, then point `DST_WEB_DIST` at the resulting `dist/` |
| `DST_ENVIRONMENT` / `DST_LOG_LEVEL` | `local` / `INFO` | |
| `DST_EDITION` | `oss` | UI badging only; core behavior never gates on it |
| `DST_GCP_CREDENTIALS` / `DST_GCP_PROJECT` | — | BigQuery service-account JSON path + project (env-configured connections; declared connections carry their own) |
| `DST_BIGQUERY_MAX_BYTES_BILLED` | `10000000000` | hard cap per BigQuery query |
| `DST_DUCKDB_JAFFLE_PATH` | `fixtures/jaffle_shop.duckdb` | the bundled demo warehouse |
| `FASTEMBED_CACHE_PATH` | `~/.cache/dst/fastembed` | where the `local` embedder keeps its ONNX model weights (fastembed's own variable, honored unprefixed). The default is deliberately **not** fastembed's own, which is the OS scratch directory the OS can reap out from under a running server; a model cache is not scratch space, so it lives under the user cache directory (`XDG_CACHE_HOME` honored). In a container, point it at a mounted volume (`services/context/local_embedder.py`) |
| `CLERK_SECRET_KEY` / `CLERK_PUBLISHABLE_KEY` | — | optional hosted dashboard auth; the local login + admin token work without it |
| `DST_OIDC_ISSUER` | — | enables generic OIDC login: point at any standard IdP (Keycloak, Authentik, Zitadel, Okta, Entra, Google). Sits beside Clerk and local login; unset = off |
| `DST_OIDC_AUDIENCE` | — | the token's expected audience (usually the OIDC client id). **Required** when the issuer is set: verifying without it is the "any tenant's token validates" hole, so tokens are refused until it is present |
| `DST_OIDC_JWKS_URL` | — | override the signing-key URL; otherwise discovered from `<issuer>/.well-known/openid-configuration` |
| `DST_OIDC_GROUPS_CLAIM` | `groups` | the token claim carrying the user's groups/roles, mapped to caller groups so lens allow-lists grant by group |
| `DST_OIDC_ADMIN_GROUP` | — | the group value that grants admin. Unset ⇒ OIDC users are never admin (safe default: name the privileged group deliberately) |
| `DST_OIDC_ORG` | `oidc` | display name for the org all users of one issuer share |

Generic OIDC is included; SAML and SCIM are not. A common self-host shape is an auth
proxy (oauth2-proxy) terminating the browser redirect and forwarding the token, so
the backend's job (verify issuer + audience + signature, map groups) is the whole
integration.

### Names and compatibility

Settings read `DST_<NAME>`. They used to read the bare `<NAME>`, which on a shared
machine is a collision: `ENVIRONMENT`, `SECRET_KEY`, `EDITION`, `PROVIDERS` and
`PROJECT_FILE` belong to half the ecosystem, and an ambient `ENVIRONMENT=production`
can silently rewrite both DSNs. The bare names still work, but are deprecated: they
are read at lower precedence than the prefixed one, and an install still using them logs
one deprecation line at startup naming what to rename.

Five names are **not** deprecated, because they are somebody else's convention that
dst answers to on purpose:

| Var | Why it stays unprefixed |
|---|---|
| `DATABASE_URL` | every PaaS (Heroku, Render, Railway, Fly) injects it under that name |
| `DATABASE_ADMIN_URL` | its documented pair in every deploy artifact here; no platform injects the admin half |
| `CLERK_SECRET_KEY` / `CLERK_PUBLISHABLE_KEY` | Clerk's own names, set for you by its hosting integrations |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | the spelling a Next.js frontend already exports; read as a third alias of `CLERK_PUBLISHABLE_KEY` |

The first four also accept a `DST_`-prefixed form, which wins when both are set:
the override for a platform-injected `DATABASE_URL` that points at the wrong database.
(`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` has no prefixed spelling — it is an alias, not a
setting of its own.)

### Plugins

A package extends the API by declaring an entry point in the `dst.plugins` group
resolving to `register(app)` (`services/plugins.py`). Plugins load after every core
router, so they can never shadow a core route, and one that raises is logged and
skipped: the core always boots.

Installing one changes what your API serves, so it is never silent:

- each mount logs its name, distribution, version and how many routes it added;
- `/ready` reports `plugins`: `"cloud@0.0.1"`, or `"none"`. It is not a readiness
  gate: a plugin is an extension, not a dependency.

By default every installed plugin mounts. Set `DST_PLUGINS=name1,name2` to pin the
route table to exactly those names; anything else installed is refused with a warning
naming it. `DST_PLUGINS=` (empty) means none, and says so.

### Per-user warehouse identity (the credential seam)

By default dst connects to your warehouse as **one service account per org**:
the managed default, and what most deployments want. If you need each query to run
under the *individual* who asked (so your warehouse's own audit log, row policies, and
masking resolve to the real person), the credential used to build a connector is
overridable.

dst does **not** ship the mapping from a dst person to a warehouse principal:
that is your identity system's job (Okta, an internal automation, a Vault of per-user
Snowflake keypairs), and baking a specific one in would be the wrong layer. What
dst gives you is the seam: the caller identity reaches the point where the
credential is chosen, and you return whatever that person should connect as.

Register a resolver from a plugin's `register()`:

```python
from services.lenses import credential_resolver

def per_user(req):
    # req.caller is the person (None on the admin/mgmt plane);
    # req.connection_type, req.config describe the warehouse;
    # req.org_secret is the org service account — the default.
    if req.caller and req.connection_type == "snowflake":
        return my_vault.snowflake_key_for(req.caller.name)  # your automation
    return req.org_secret

def register(app):
    credential_resolver.set_resolver(per_user)
```

Return `req.org_secret` for any caller you don't handle: the default is always in the
request, so an override is additive and never a cliff that drops someone's access.
This is integration work, not a checkbox; the seam is that it is *possible* and
supported, not that dst does the federation for you.

## Client-side variables

Read by the CLI and MCP clients, not the server (`services/cli/main.py`,
`services/mcp/server.py`):

| Var | Purpose |
|---|---|
| `DST_URL` | server URL for every remote CLI command, and the port `dst dev`/`serve` bind. `dst init` writes it |
| `DST_ADMIN_TOKEN` | admin token for remote CLI commands. `dst bootstrap` writes it into `./.env` |
| `DST_API_KEY_<NAME>` | the convention for provider/connection secrets declared via `api_key_env` / `secret_env` |
| `DST_API_KEY` | a caller key for the **stdio** MCP server only (`python -m services.mcp.server`); the remote transport reads the key from the request header |

*(Keys and defaults verified against `services/config.py`,
`services/project/schema.py`, and `services/cli/main.py`.)*
