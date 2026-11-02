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
| `ai_pricing` | per-model prices, `{"model": [usd_per_mtok_in, usd_per_mtok_out]}`, optionally followed by the prompt-cache read, cache write and long-lived cache write rates (`[in, out, read, write, write_long]`; the long-lived write is Anthropic's 1-hour cache, OpenAI's 12-hour). A key `"<endpoint>:<model>"` (`"openai-decisions:gpt-6-luna"`) prices the model only when called through that endpoint. **This is the authority.** dst ships a dated fallback list (`AI_PRICES_AS_OF` in `services/observability/cost.py`) so a fresh install traces something, but a released package cannot track vendor pricing, your negotiated rate, or a model newer than it — declare the models you actually run. A model absent from both traces as unpriced, and its whole trace's cost is `NULL`, never a default. The list covers every model the built-in provider types resolve to by default, Anthropic's and OpenAI's current models from their published pricing pages (standard rate; Claude Haiku 5.5's prompts over 100,000 tokens at its long-prompt rate), and a dated snapshot id (`claude-haiku-4-5-20251001`) prices as its model. Any model your providers can call with no price is named on `/ready` (`ai_pricing`) and by `dst doctor`, and logged the first time it is called. Prompt-cache reads and writes are priced at the model's cache rates from the same pages; a call that reports cache tokens its model has no cache rate for is unpriced, never charged as if they were free, and an `anthropic` provider's model with no cache rate is named on `/ready` and by `dst doctor` (that client caches its system prompt). A model is priced by the endpoint it is called through where that changes the price: `gpt-6-luna` over the Decisions API bills input only, over chat completions input and output |
| `connections` | warehouse declarations, keyed by name |
| `demo` | what a public demo's `/demo` page shows besides what dst derives itself; see [Demo page](#demo-page-demo) |
| `probe` | how `dst probe` samples and how old its profile may get; see [Probe](#probe-probe) |
| `lineage` | where the dbt or SQLMesh model DAG lives, for `table/X+` selection; see [Lineage](#lineage-lineage) |
| `cube` | the Cube this project compiles governed asks through; see [Cube](#cube-cube) |
| `hand_off` | where a question that is not dst's goes: reports, dashboards, a ticket queue; see [Hand-off](#hand-off-hand_off) |
| `groups` | the caller groups a `hide` entry may except, e.g. `[support, sales]`; plan and apply fail on an `except_groups` name not listed. Apply stores the list per org, and a pushed `dst.yaml` without it clears it. Declaring a group grants nothing: membership is set on the caller ([Security](../security.md#hidden-columns-and-group-exceptions)). The mapping form `{support: {idp: [cs-team]}, ops: }` declares the same names and which identity-provider groups grant each; an IdP group named nowhere grants nothing |

### Provider fields (`providers.<name>`)

No vendor is special-cased: the core knows wire shapes, your config knows vendors
(`services/config.py`). Declaration order is the tier/cost preference: put the cheap
provider first.

| Field | What |
|---|---|
| `type` | wire protocol: `anthropic`, `openai-compatible` (covers OpenAI, DeepSeek, Ollama, vLLM, Groq, most gateways), `local` (in-process embeddings, no key; the `dst-core[local-embed]` extra), or a typed-decision provider, `typesafe` or `openai-decisions` (closed-set decisions with a probability per option — [Typed decisions](../concepts/typed-decisions.md)) |
| `api_key_env` | name of the env var holding the key (convention: `DST_API_KEY_<NAME>`; `openai-decisions` reads `OPENAI_API_KEY` when this is unset); the only right choice in a committed file |
| `base_url` | API base URL; required for `openai-compatible` |
| `fast_model` | this provider's cheap first-pass model; the first declared provider with one carries the fast tier |
| `smart_model` | this provider's quality model |
| `models` | extra model names this provider serves, for bare-ref resolution; for `openai-decisions`, the first one is the decision model (default `gpt-6-luna`) |
| `embedding_model` | embedding model this provider serves (`openai-compatible` uses `POST {base_url}/embeddings`) |
| `embedding_dim` | embedding vector dimension (default 1024). Changing it on an install that already holds vectors requires `dst reindex`; the write-path guard blocks mismatched writes until then |
| `reasoning` | do this provider's models bill their thinking against `max_tokens`? Leave unset and dst decides per model name; set `true`/`false` to override; see below |

#### The three embedding postures

Embeddings power certified-answer matching and routing, and the scaffold spells
out three postures (`services/cli/init.py`):

- **poc**, `{type: local}`: in-process, no API key
  (`pip install 'dst-core[local-embed]'`); matching bands auto-adjust.
  `dst init` writes this entry when the extra is installed.
- **production**: an `openai-compatible` entry with `embedding_model` +
  `base_url`.
- **none**: omit it entirely. Generation, guards and evals still work, but
  certified matching cannot run and routing falls back to lexical. Every answer
  on a lens that holds certified answers then carries a `DEGRADED` line saying
  certified matching is off, how many of the lens's certified answers cannot
  serve, and what to install or declare: degraded visibly, never silently
  wrong. A lens with no certified answers gets no line. A configured embedder
  that fails puts a line naming the failure on every answer. `dst plan` and `dst apply` also
  print a `DEGRADED` line on every lens that declares certified answers,
  naming what is missing (no provider, or a declared provider whose extra or
  key this install lacks).

The serve bands (the similarity a question needs to serve an approved answer
outright, to be checked as a paraphrase, or to be used as an example) are
measured per embedding model. `BAAI/bge-small-en-v1.5`, the local default, has
its own; any other model runs on the defaults (0.95, 0.90, 0.83), and plan,
apply and `/ready` (`certified_bands`) say that the bands were not calibrated
for it. A model whose similarities spread wider than the defaults assume can
generate answers for paraphrases that should have served. `dst calibrate-bands`
fits the bands for the configured model from labelled paraphrase pairs and stores
them for that model; a stored fit wins over the built-in bands
([CLI](cli.md)).

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
| `bigquery` | `project`, `dataset`, `datasets`, `schema`, `max_bytes_billed`, `sample_bytes_budget` |
| `snowflake` | `account`, `user`, `warehouse`, `database`, `schema`, `role`, `auth`, `private_key_passphrase` |
| `postgres` | `host`, `port`, `database`, `user`, `schema`, `sslmode`, `statement_timeout_ms` |
| `mysql` | `host`, `port`, `database`, `user`, `statement_timeout_ms` |
| `duckdb` | `path` (a file, or `md:<database>` for MotherDuck), `schema`, `read_only`, `statement_timeout_ms` |

**The cost and time caps belong here**, not in a shell: `max_bytes_billed` (BigQuery,
default 10 GB) and `statement_timeout_ms` are per-connection, so a wide-scanning lens
can be raised without loosening the cap on a connection that should never scan wide —
and the value is reviewed in the repo instead of living in one operator's environment.
`DST_BIGQUERY_MAX_BYTES_BILLED` still overrides globally.
`sample_bytes_budget` (BigQuery, default 2 GB) is the separate cap on one probe sampling
query: a table whose dry-run estimate exceeds it is left unsampled (its catalog profile
stands) and the probe says so — raise it here, or narrow the probe with `--tables`. The
profile records why. Such a table has no value dictionaries, so no value of its columns
can bind from one: describe says so on each entity that reads it (`values_note`), and a
question asked for a value of one of its columns gives the same reason.

A wide-warehouse BigQuery connection, fully scoped:

```yaml
connections:
  bigquery:
    type: bigquery
    config:
      project: data-platform-production
      datasets: [finance_marts, product_marts, revops_marts]
      max_bytes_billed: 50000000000   # 50 GB — this warehouse's wide scans are real
    secret_env: DST_API_KEY_BIGQUERY
```

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

A typed-decision provider is one more entry; with it configured and
`DST_TYPED_SERVING=auto` (the default), every lens serves typed and the router
decides over every published lens with no similarity shortlist:

```yaml
providers:
  jev:
    type: typesafe
    api_key_env: DST_API_KEY_JEV
```

OpenAI's Decisions API is the other typed-decision type. It reads
`OPENAI_API_KEY` unless `api_key_env` names another variable, calls
`gpt-6-luna` unless `models` names a model, and takes a `base_url` for a
gateway (default `https://api.openai.com/v1`):

```yaml
providers:
  openai:
    type: openai-decisions
```

With more than one typed-decision provider declared, the first one in
declaration order that has a key decides; a keyless one is skipped.

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

### Demo page (`demo`)

A deployment in [public demo mode](api.md#public-demo-mode) serves a sign-in
page at `/demo`. The page lists what visitors can ask from the published lenses' own
display names and common questions; the `demo:` section adds what only the operator
knows. Every key is optional, and an absent key renders nothing.

| Key | What |
|---|---|
| `tagline` | the one sentence under the instance name. Without it the page says "Ask your questions from Claude, ChatGPT or any AI you use." |
| `example` | an example conversation, shown folded after sign-in: a list of turns, each exactly one of `user` (what the person asks), `tool` (the tool-call line, conventionally `asked <instance> — <question>`), `assistant` (the reply) or `receipt` (lines under a reply, `label: value`, shown in the order written) |
| `privacy_url` | the operator's privacy notice, linked in the fine print; an `http(s)` URL |
| `log_days` | how long questions stay in the request log, stated in the fine print. dst does not prune on its own; schedule `dst prune-log --keep-days <n>` to make it true |
| `audience` | `engineer` (default) or `consumer`. Under `consumer`, a demo caller's answer on every door (REST, the OpenAI-compatible endpoint, MCP) is the answer text with its scope and freshness lines, its `status`, a `clarification` or a refusal's reason, `data_as_of`, `truncated` and `request_id`. Nothing else is on the wire: no SQL, rows, citations, checks, grade, certification, receipt, trust summary, degraded lines or resolution ledger. Those are the operator's, and the request log keeps all of them, so `dst observe` and the dashboard are unchanged. Other callers in the org, and every other deployment, get the whole answer |

```yaml
demo:
  tagline: Ask about a year of pro Dota 2 from Claude, ChatGPT or any AI you use.
  privacy_url: https://www.example.com/privacy/
  log_days: 30
  example:
    - user: I want to learn a carry this patch. What's strong?
    - tool: asked roshan — which carry heroes win their lane most often on the current patch?
    - assistant: >-
        Shadow Fiend stands out: pro carries on him win their lane 59.1% of the time
        this patch, ahead of Magnus and Drow Ranger.
    - receipt:
        lane win: 400 gold ahead at minute 10
        scope: premium and professional leagues
```

`dst plan` and `dst apply` validate the section with the rest of `dst.yaml`: an unknown
key, an empty turn, or a turn with two kinds is an error that names the key. Apply
stores it per org, and a pushed `dst.yaml` without the section clears it. Only a
deployment whose `DST_DEMO_ORG_ID` is the org being applied shows it; anywhere else
apply says so in a warning.

### Probe (`probe`)

`dst probe` writes `profiles/<connection>.probe.json`, the warehouse's measured
profile: row counts, partitions, freshness stamps, value dictionaries, date spans.
`dst apply` lands it in the store serving reads. The user owns the probe clock (a
scheduled job, a deploy step); this section is how dst notices when it stops.

| Key | What |
|---|---|
| `max_age` | how old the newest profile in scope may be before every surface says so. A duration: `7d` (the default), `48h`, `30m`; `0` turns the age rails off. Past it, an answer carries `PROFILE: last probed N days ago (max M) — run dst probe` in `degraded`, the freshness and date-coverage checks skip instead of failing on a stamp that only dates the last probe, the value guard consults no dictionary (a stale one cannot prove a new value absent), `dst observe` and `/ready` carry the oldest age and the stale connections, `dst plan` and `dst apply` warn when the pushed artifact is older than this, and `dst test --all` skips the lens and exits 4 |
| `recent_partitions` | how much of each table the sampling pass reads on a refresh: the latest `n` partitions of a time-partitioned table, or the last `n` days of the entity's `default_time_field` for an unpartitioned one. Default 7; `0` reads whole tables. The catalog pass (every table, row counts, whole-table freshness and date span) is never narrowed. A table the stored artifact never read whole (the first probe of a connection, a table an earlier run did not sample) is read whole, so the dictionaries it proves complete are kept through every windowed refresh as the dated union; the artifact records the slice per table under `sample_window`. A refresh that leaves a dictionary partial which the previous probe held complete names it in a warning |
| `cube.max_queries` | the most `/v1/load` queries one `dst probe --cube` or `dst monitor` run makes. Default 500. What the budget leaves unread is listed in the artifact and printed, never dropped silently |
| `cube.max_values` | the most values read for one dimension's dictionary through Cube. Default 500; a dimension holding more is recorded incomplete |
| `drift_tolerance` | how far a column's measured distribution may move between the drift baseline (`profiles/<connection>.json`) and the newest probe artifact before `dst drift` reports it as breaking. `time_shift` bounds a time column's median offset from the entity's `default_time_field` (default `1h`; `30m`, `1d`; `0` reports any move). `numeric_shift` bounds a numeric column's mean, relative to the baseline (default `0.2`, twenty percent; `0` reports any move). A complete dictionary losing a member is never within tolerance; a changed column comment is reported as comment only and never breaks |

```yaml
probe:
  max_age: 7d
  recent_partitions: 7
  cube:
    max_queries: 500
    max_values: 500
  drift_tolerance:
    time_shift: 1h
    numeric_shift: 0.2
```

Apply stores the section per org, so a server reads the limit the project declared,
and a pushed `dst.yaml` without the section stores the defaults.

### Lineage (`lineage`)

The transformation tool whose model DAG a `table/X+` selector walks
([Tables and the model DAG](cli.md#tables-and-the-model-dag)). Exactly one key, a path
relative to the project root:

| Key | What |
|---|---|
| `dbt` | a dbt target directory holding `manifest.json` (`dbt parse` writes it), or the dbt project directory whose `target/` holds it |
| `sqlmesh` | a SQLMesh project directory; its `models/` files are read |

```yaml
lineage:
  dbt: ../transform/target
```

The CLI reads the DAG and sends it with the selection; the server stores nothing.
`--lineage PATH` on `dst plan`, `dst apply` and `dst test` overrides the key. A lineage
that is configured but cannot be read (no `manifest.json` yet, no `models/`) is an error
before any request, never a walk over an empty graph.

### Cube (`cube`)

The Cube (Cube Core, the sidecar or your own) whose model dst reads. dst reads the
model only through Cube's REST API (`/cubejs-api/v1/meta`) and never parses the model
files. Every call carries a security-context token: an HS256 JWT signed with
`CUBEJS_API_SECRET`, holding `{dst: {org, caller, groups, lens, request_id}}` and valid
for five minutes, so an `access_policy` in the model can read
`security_context.dst.groups`. With the block declared and `cube/overlay.yaml` in the
project, the lenses the overlay lists (or that name `views:`) serve through Cube unless
their `serving:` says otherwise ([Lens serving](#lens-serving-and-views)). `dst cube
status`, `dst doctor` and `/ready` report on it.

| Key | What |
|---|---|
| `url` | Cube's base URL, e.g. `http://localhost:4000` |
| `api_secret_env` | the env var holding `CUBEJS_API_SECRET`. Default `CUBEJS_API_SECRET`. Named, never inlined: an `api_secret` key is an error |
| `meta_ttl` | seconds a fetched meta document is reused for the same caller groups before Cube is asked again. Default `300`; `0` asks every time |
| `jwt_clock` | the clock each request's token is dated by. `wall` (default) is the real time even when dst's own clock is pinned to another day, as tests and replays pin it, so Cube never finds the token expired; `process` is the clock Python reports |
| `plan_context.groups` | the caller groups plan and apply read the meta as. Empty, plan reads it as a caller in every group the overlay's `hide` entries except, so the members Cube withholds from everyone else are still checked; it also reads it as a caller in no group, to measure what Cube enforces |

```yaml
cube:
  url: http://localhost:4000
  api_secret_env: CUBEJS_API_SECRET
  meta_ttl: 300
```

`DST_CUBE_URL`, `DST_CUBE_API_SECRET_ENV`, `DST_CUBE_META_TTL` and `DST_CUBE_JWT_CLOCK` override the block
field by field.

### Hand-off (`hand_off`)

Where a caller is sent for what dst does not do. A question that asks which report
shows a figure, or asks for an errand (email this, export that, schedule it), is
answered with these entries under the `hand_off` or `not_a_data_question` mode
([Question types](../concepts/question-types.md)). dst keeps no inventory of reports
and reads nothing behind the entries; it only names them. With no `hand_off`
declared, a "what report shows" ask is refused saying so.

| Key | What |
|---|---|
| `name` | what people call it, e.g. `Sales dashboard` |
| `kind` | what it is: `dashboard`, `report`, `bi`, `ticket`, `docs`, `chat` or `other` |
| `url` | where to open it |
| `mcp` | its MCP endpoint, for an agent. One of `url` or `mcp` is required |

```yaml
hand_off:
  - name: Sales dashboard
    kind: dashboard
    url: https://bi.example.com/sales
  - name: Helpdesk
    kind: ticket
    mcp: https://helpdesk.example.com/mcp
```

## Entity rails (`semantic/entities/<name>.yaml`)

The deterministic behaviour levers on an entity, enforced by the compiler and the
serve-time checks, not by hoping the model reads prose (see the
[project-files guide](../guides/project-files.md#the-deterministic-rails-on-an-entity)
for the full mechanism table):

| Key | Type | What it enforces |
|---|---|---|
| `population` | string | Declares who/what the rows cover; the `population_declared` serve check requires answers to carry the scope |
| `population_filter` | SQL predicate | The compiler ANDs it into **every** query against the entity; scope bound no phrasing can smuggle past |
| `pinned_dimensions` | list of fields | Each must be pinned or GROUPed before any aggregate: the compiler refuses an unpinned aggregate naming the dimension and its values, and the `aggregation_scope` check holds generated SQL to it; "never sum across currencies", structurally |
| `filters:` (on a metric) | SQL predicates | Part of the metric's meaning; the filter guard rejects the metric computed without them |
| `agg: sum_distinct_on` + `distinct_on:` (on a metric) | aggregate + list of SQL expressions | Sums one value per distinct key (a monthly quota on daily rows counted once per rep-month); plan rejects it without `distinct_on` |

## Table blueprints (`semantic/tables/<table>.yaml`)

Facts about one warehouse table, one file per table, project-wide
([guide](../guides/project-files.md#table-blueprints-facts-about-a-table)). The
scaffold's `semantic/README.md` carries the reference block rendered from the schema
(`TableBlueprint` in `services/contracts/semantic_model.py`):

| Key | Type | What it declares |
|---|---|---|
| `connection` | string | the `dst.yaml` connection the table lives on; one `dst.yaml` does not declare is a plan error |
| `table` | string | the table as its connection names it (`dst introspect` prints it); the identity, one blueprint per table name |
| `system` | string | the source system the table is loaded from, as the team names it. Plan never hints a join between tables of two different systems (case, spaces and punctuation ignored) |
| `grain` | string | one line: what one row is. Inherited by an entity on the table that declares none, and rendered into both generation prompts |
| `key` | list of columns | the columns one row is unique on. Plan and apply count it against the rows (probe or one live count) and fail on a duplicate; an entity with no `primary_key` inherits it when it reads every key column |
| `versions.sync_column` | column | set when rows repeat once per sync of a record: the column that stamps the sync. Uniqueness is then checked on the key plus this column, and an answer's grain line counts the key without it |
| `versions.latest_only` | list of columns | columns that hold on a record's latest version only, such as a delete flag |
| `clocks.<column>.meaning` | string | what the timestamp records |
| `clocks.<column>.timezone` | IANA zone | the zone the values are stored in; one no database knows is a parse error |
| `joins[].to` | string | another table on the same connection |
| `joins[].on` | SQL condition | columns qualified by the bare table names; type the key as `"on":` or `condition:` (a bare `on` is a YAML boolean) |
| `joins[].cardinality` | `one` \| `many` | rows of `to` per row of this table |
| `caveats` | list of strings | what a reader of the table must know, one sentence each; rendered into both prompts and shown by `describe_lens`. A column the lens hides reads `[withheld column]` there, and plan warns |

An unknown key is a parse error naming the file. Two files for one table are an
error naming both. A check that fails aborts apply only for a blueprint the push
adds or changes; a stored one that stops holding is a `DRIFT:` warning. A live
count runs on a billed warehouse only when its dry-run estimate is within the
connection's `sample_bytes_budget`, and serving reuses it for `probe.max_age`
(one hour when that is `0`).

## Lens serve-time contracts (`lenses/<name>/lens.yaml`)

These lens keys declare contracts the server enforces at serve time; the
scaffold's reference block carries the full key list
(`services/contracts/lens_config.py`):

| Key | Default | What it declares |
|---|---|---|
| `stale_after_days` (top level) | unset | the freshness contract: days after the scope's measured last-update at which answers flag stale: the freshness check fails, the grade caps at `partial`, and the answer says so. Unset = undeclared; the check reports skip, never a vacuous pass. Read [how `data_as_of` is measured](../guides/project-files.md#freshness-is-a-declared-contract) before setting it |
| `not_for` (top level) | `[]` | asks this lens must refuse although its tables could produce a figure, each `{ask, sibling}`: a question saying the ask as a phrase (its content words in order) is refused before typing or generation, naming the sibling lens; the longest matching ask wins, and a one-word ask inside one of the lens's own metrics never matches. `Avoid for:` lines in a single entity's `use_cases` are read the same way |
| `sources` (top level) | `[]` | the source systems the lens's data comes from, as people name them, e.g. `[ticketing, door scanners]`: a question naming a system some lens declares routes only to lenses declaring it, and is uncovered, naming the system, when no lens the caller may use does. Whole words, case ignored; an entry twice or with no letter or digit is an error, and one spelled like a metric of the lens is a plan warning |
| `scope` (top level) | unset | whose figures the lens holds: `own` or `market`. Shown to the routing decider |
| `model.answer_contract` | `strict` | the output-grid contract in every generation prompt: project exactly the asked quantities at the asked grain, full precision. `off` removes the block (a bare `off` is fine; YAML 1.1 parses it as a boolean and the loader coerces it back) |

### Lens serving and views

Which core answers a lens, and, for a lens served through Cube, the views it serves:

| Key | Default | What it declares |
|---|---|---|
| `serving` (top level) | unset | `cube`: questions resolve to a Cube query over the lens's views, checked before and after execution, and run through the lens's connection. `typed`: the typed resolver over the entity model. Unset, a lens of a Cube-backed project serves `cube` and every other lens `typed`. A lens is Cube-backed when the push carries `cube/overlay.yaml` and either the overlay lists it under `lenses:` or its `lens.yaml` names `views:`; apply stamps it against Cube's model. Without the overlay no lens is Cube-backed, and a push without it clears the stamp. `DST_SERVING` overrides this key for every lens of an install |
| `views` (top level) | `[]` | the Cube views this lens serves, in a project with `cube/overlay.yaml`; overrides the overlay's `lenses.<lens>.views`. Plan reads them from Cube's `/v1/meta` and apply stamps the `model_version` they compile to |

`dst plan` prints the lane of every lens in the push on one line,
`serving: <lens> → <lane> (<why>)`, where the reason is `DST_SERVING`, `lens serving:`,
`Cube-backed` or `default`. `dst doctor` and `/ready` print it per published lens
([Migrate to Cube](../guides/migrate-to-cube.md)).

### Tables without definitions (`expose`)

A lens may read tables no entity file describes
([guide](../guides/project-files.md#exposing-tables-that-have-no-definitions)).
The grant is a list under `expose:`, resolved at apply against the connection's
stored profiles:

| Key | Type | What it declares |
|---|---|---|
| `expose[].connection` | string | the dst.yaml connection whose profiled tables the patterns match; must be in the lens's `connections` |
| `expose[].tables` | list of patterns | fnmatch-style on the table name as introspect names it: `schema.table` (`staging.*`, `analytics.dim_*`), `project.dataset.table` on BigQuery (`*.analytics.dim_*`); a pattern matching no profiled table fails apply; a table whose bare name a selected entity or another exposed table shares is exposed under its qualified name (`staging_orders`) |
| `expose[].hide` | list of patterns | columns withheld, matched as `table.column` bare or qualified (`*.ssn`, `staging.customers.email`); a hidden column is out of scope |

Each matched table compiles to an entity with the profile's columns and nothing
else. Answers from it are disclosed `EXPOSED`, never typed, and never `declared`
in the ledger; with `untyped_fallback: false` the `query` door cannot serve them
(plan warns), while the `sql` tool reads them directly. Every answer that reads one
also carries a `GRAIN` line per table, read from the table's
[blueprint](#table-blueprints-semantictablestableyaml): rows against distinct key,
or "could not verify" and why.

## Cube overlay (`cube/overlay.yaml`)

In a Cube-backed project, what dst reads that no member's `meta.dst` carries. The file
`dst init --cube` scaffolds ends with a reference block of every field, and the
`dst-cube-overlay` skill maps each one to what reads it. An unknown key is a parse
error naming the file, the key path and the nearest valid key.

| Key | What it holds |
|---|---|
| `version` | `1` |
| `cubes.<cube>` | per cube: `default_time_dimension` (its clock), `use_cases` and `common_questions` (routing text), `caveats` and `member_caveats` (carried by every answer that reads the cube or member), `pinned_dimensions`, `clock_kind` (`event` or `schedule` per time dimension), `feed_axes` and `feed_measures` (what the monitor judges as feeds), `loaded_at` (the time dimension stamping each row's load, the monitor's evidence that a short month is a partial load), `matures_after` (measure → days after a period ends before its values are final: a short month whose load evidence says complete is disclosed, within those days, as complete with values still maturing, never as partially loaded), `entity_scope` (a one-company cube and its group-level counterparts) |
| `lenses.<lens>` | per lens: `views`, and its policy over them: `owner`, `timezone`, `stale_after_days`, `scope`, `sources`, `not_for`, `not_computable`, `access`, `expose`, `eval_gate`, `auto_review`, `definitions` (the overlay definitions it reads, by term) |
| `definitions` | prose definitions the model cannot carry as a segment: `metric`, `about` (a cube or member), `summary`, `aliases`, `status`, `audiences`, `file` |
| `ambiguous` | ambiguous terms, the same fields plus `readings` (each `says`, `members`, `select_words`, `default: true`) and `default` |
| `hide` | dimensions withheld from every caller outside the groups: `cube`, `dimension`, `except_groups` |
| `scope` | the words that scope a figure to one company: `names`, `group_names`, `axes`, `word_axes` |

An ambiguous term's `default` names one reading by its `says`. With it declared, that
reading is served when the question names none, however far the others are, and the
others are disclosed beside it (with their values when they compute). Without it, the
caller is asked when the readings differ by more than 1 %; readings within 1 % serve the
one marked `default: true`, else the one the query read. A `default` that names no reading, or disagrees with the
reading marked `default: true`, is a parse error.

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
| `DST_TYPED_SERVING` | `auto` | `auto`: typed serving whenever a typed-decision provider (`typesafe` or `openai-decisions`) resolves; `on`: typed serving on any decider (a voting chat model, five samples per decision); `off`: the one-shot intent emission ([Typed decisions](../concepts/typed-decisions.md)) |
| `DST_SERVING` | unset | `typed` or `cube`: the serving core for every lens of the install, over each lens's own `serving:` and the Cube-backed default. Unset, each lens decides ([Lens serving](#lens-serving-and-views)) |
| `DST_GATE_CONCURRENCY` | `16` | workers for the test lanes and the apply gate — the slot lane's wall time is the provider's round trips divided by this |
| `DST_DEFAULT_PROVIDER` | first declared | bare-model-ref fallback |
| `DST_LLM_DESCRIPTIONS` | `true` | the profiling chain's LLM description pass, which sends table/column names and sampled values to the configured provider to fill undocumented columns. `false` turns it off; profiling then stays entirely between dst and the warehouse. See [Security and data flow](../security.md) |
| `DST_AI_PRICING` | `{}` | per-model pricing JSON, same shape as the file key |
| `DST_PROJECT_FILE` | `dst.yaml` | the workspace config, relative to the server's cwd |
| `DST_CUBE_URL` / `DST_CUBE_API_SECRET_ENV` / `DST_CUBE_META_TTL` / `DST_CUBE_JWT_CLOCK` | unset | the [Cube](#cube-cube) block's fields, set from the environment; each one set here wins over `dst.yaml` |
| `DST_PLUGINS` | unset | entry-point plugins allowed to mount routes, comma-separated by name. Unset means every installed one mounts; set means exactly these, and an installed plugin outside the list is refused with a warning naming it. Either way every mount is logged and listed in `/ready`; see [Plugins](#plugins) |
| `DST_SERVING_TIMEOUT_S` | `600` | bound on each per-request model/embedding call and each warehouse step (the dry run, the query, a value probe) while serving a query. Generous on purpose: the slowest legitimate generations run for minutes; it exists so a wedged provider (or an embedder cold start downloading model weights) surfaces as an error naming the stage instead of a request that never returns. A warehouse step past it answers 504 naming the step, and a DuckDB or MotherDuck statement past it is interrupted (on MotherDuck, one that does not stop within 5 s gives up its connection to a fresh one); later requests are unaffected. Each warehouse step logs its name, its connection and its seconds at INFO. `0` disables the bound |
| `DST_APPLY_STEP_TIMEOUT_S` | `60` | deadline on each warehouse-touching step of `dst apply`: opening a declared connection, probing it, running a certified answer's or an eval case's SQL. Apply holds the org's apply lock and its transaction for the whole request, so a warehouse that accepts the connection and never answers would otherwise hold both until the process dies. Past the deadline the apply returns a 504 naming the step, rolls back, and the next apply proceeds. Each step logs its name and seconds at INFO. `0` disables the bound |
| `DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S` | `300` | bound on a warehouse's one-time cost in a server process: the first open of a MotherDuck database, which also loads the MotherDuck extension (a download on a machine that never loaded it) and attaches. A warm machine opens in well under a second. `dst apply` pays it before the step deadline above starts, and serving holds a first open to this bound instead of the 60 s one on a routine open, so both deadlines measure the warehouse rather than the download. A first open past it aborts the apply with a 504 naming the open. `0` disables the bound |
| `DST_PUBLIC_BASE_URL` | — | public base URL for review tracking links and OAuth metadata behind a proxy |
| `DST_CORS_ORIGINS` | — | extra origins, comma-separated, for split frontend/backend deploys |
| `DST_TOKEN_DEFAULT_EXPIRY_DAYS` | — | expiry applied to newly issued `dst_` caller keys. Unset = the key never expires |
| `DST_TOKEN_MAX_EXPIRY_DAYS` | — | hard cap on caller-key lifetime. Applies to every mint, including one that explicitly requests longer; the request is **capped, not refused**, so an operator gets a usable key rather than none |
| `DST_WEB_DIST` | auto-detected | directory of the built dashboard SPA; `dst serve` finds the source build or the wheel's bundled copy on its own. When **neither** exists (any wheel built without the web step is API-only), startup says `dashboard: not bundled in this install` and every CLI command still works; to add the UI: `pnpm -C apps/web install && pnpm -C apps/web build`, then point `DST_WEB_DIST` at the resulting `dist/` |
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
| `DST_OIDC_CLIENT_ID` | the audience | the client id dst is registered under at the IdP, for the MCP consent page's sign-in; the audience is the client id at every common IdP, so it is rarely needed |
| `DST_OIDC_CLIENT_SECRET` | — | set when the IdP registered dst as a confidential client; a public PKCE client needs none. Sent to the token endpoint only |
| `DST_OIDC_SCOPES` | `openid email profile` | the scopes the sign-in requests. Okta puts the groups claim in the ID token only when `groups` is added; Keycloak and Entra reject a scope they do not define |
| `DST_OIDC_TOKEN_DAYS` | `30` | lifetime of an MCP token minted after an IdP sign-in |
| `DST_OIDC_USERS_URL` | — | a URL listing the IdP's current users as JSON, read by `dst keys sync-idp`: Okta `https://<org>.okta.com/api/v1/users?filter=status eq "ACTIVE"`, Keycloak `https://<host>/admin/realms/<realm>/users?enabled=true`. Put the "still active" filter in the URL; a listed user is kept, an unlisted one is revoked. `Link: rel="next"` pages are followed |
| `DST_OIDC_USERS_TOKEN` | — | bearer token for that URL (an Okta API token, a Keycloak service-account token) |
| `DST_OIDC_USERS_SUBJECT_FIELD` | `id` | the field of each listed user holding the subject the `sub` claim carries; `id` at Okta and Keycloak |

Generic OIDC is included, on the dashboard and on the MCP consent page; SAML and SCIM
are not. The groups claim reaches dst only through the `groups:` mapping in
`dst.yaml` ([Security](../security.md#hidden-columns-and-group-exceptions)). A common
self-host shape for the dashboard is an auth proxy (oauth2-proxy) terminating the
browser redirect and forwarding the token; the MCP sign-in runs the redirect itself
and needs the IdP to allow `<DST_PUBLIC_BASE_URL>/oauth/callback` as a redirect URI.

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

Read by the CLI and MCP clients, not the server (`services/cli/_common.py`,
`services/mcp/server.py`):

| Var | Purpose |
|---|---|
| `DST_URL` | server URL for every remote CLI command, and the port `dst dev`/`serve` bind. `dst init` writes it |
| `DST_ADMIN_TOKEN` | admin token for remote CLI commands. `dst bootstrap` writes it into `./.env` |
| `DST_API_KEY_<NAME>` | the convention for provider/connection secrets declared via `api_key_env` / `secret_env` |
| `DST_API_KEY` | a caller key for the **stdio** MCP server only (`python -m services.mcp.server`); the remote transport reads the key from the request header |

*(Keys and defaults verified against `services/config.py`,
`services/project/schema.py`, and `services/cli/_common.py`.)*
