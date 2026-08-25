# Quickstart

From an empty directory to a governed answer over the bundled demo warehouse, then over
your own. Everything runs from the terminal against the scaffolded project. The
dashboard is optional throughout.

Prerequisites: Python 3.12+, Docker (for the project's Postgres), and an API key for at
least one model provider: Anthropic, or any openai-compatible endpoint (DeepSeek,
Ollama, vLLM, Groq, most gateways).

## 1. Install

```bash
pip install dst-core
dst --version
```

One package, one command: `dst`. The installed package includes the database
migrations and a bundled DuckDB demo warehouse, so nothing below needs the
source tree.

Building the package **from a source checkout** instead (a vendored snapshot, a
pinned fork)? Build the dashboard first, or you get an API-only build:
`pnpm -C apps/web install && pnpm -C apps/web build`, then `uv build --wheel`.
The build prints whether the dashboard was bundled.

## 2. Scaffold a project

```bash
dst init analytics --warehouse demo --yes
cd analytics
```

`--yes` takes the defaults instead of prompting (required when scripting; there is no
tty to answer the prompts). If ports 8000/5432 are taken, add `--api-port`/`--db-port`:
the flags write the compose mapping, the database URLs, and `DST_URL` in one pass.

`dst init` writes a project laid out the way dbt users will recognize:
`dst.yaml` (model providers + warehouse connections), a gitignored `.env` with a generated
`DST_SECRET_KEY` and local database URLs, a `docker-compose.yml` for Postgres, a
shared semantic layer — the files that describe your data — under `semantic/`
with example assets, an example lens at `lenses/customer_value/` over the demo
warehouse, an `AGENTS.md` guide for AI agents, and a `git init`. `dst.yaml` and
`lens.yaml` end with a commented reference block generated from the schema
itself: uncomment fields instead of guessing names. See
[Project files](guides/project-files.md).

Fill the one secret the scaffold declares. Open `.env` and set:

```
DST_API_KEY_ANTHROPIC=sk-ant-...
```

Secrets live only in `.env`; the tracked files refer to them by env-var name. A
key pasted directly into `dst.yaml` is a parse error, not a lint warning.

## 3. Start the server

```bash
dst dev
```

One command: it brings up the project's Postgres via docker compose if nothing
answers at `DATABASE_URL`, runs migrations, then serves the API (and the
dashboard, when bundled) on port 8000. Leave it running; the rest happens in a
second terminal in the same directory.

## 4. Bootstrap, then deploy the files

```bash
dst bootstrap --org me --email you@example.com
dst apply
```

`bootstrap` prompts for the admin password (pass `--password` to script it), creates
the org, and mints an admin token, saved into `.env` as
`DST_ADMIN_TOKEN`; every later command reads it from there, so nothing below needs
flags. `--email` also creates the first dashboard admin (log in at
`http://localhost:8000`); omit it if you don't want the dashboard yet. Rerunning
bootstrap is safe: it reuses the org and only mints a fresh token.

`apply` deploys the project directory to the server. Each declared connection
is probed first — dst connects and reads — so a dead credential never replaces
a working one. Then shared assets and lenses land in one transaction: any error
aborts everything, and the prior versions keep serving.

## 5. First governed answer

```bash
dst query customer_value "How many customers are repeat customers?"
```

The answer comes back with the SQL that produced it and a confidence grade: the
[receipts](concepts/receipts.md) every governed answer carries.

!!! clarify "Clarify"
    Ask `dst query customer_value "What is the average value of a customer?"` and
    you get a clarify prompt instead of a number: the scaffolded `value` definition is
    `status: ambiguous`, so dst asks which meaning is intended rather than guessing.
    See [Clarify & refusal](concepts/clarify-and-refusal.md).

## 6. Connect your own warehouse

Declare the connection in `dst.yaml`. Warehouse types: `duckdb`, `postgres`,
`mysql`, `bigquery`, `snowflake`; the same block also declares object stores (`s3`,
`gcs`):

```yaml
connections:
  wh:
    type: bigquery
    config: {project: my-gcp-project}
    secret_env: DST_API_KEY_WH
```

and put the credential in `.env`. A value of `@/path/to/file` loads that file's
contents — the way to pass a BigQuery service-account JSON:

```
DST_API_KEY_WH=@/path/to/service-account.json
```

Then author the semantic layer — the files that describe your data — from the
warehouse itself:

```bash
dst introspect --connection wh --profile   # schema + facts, agent-legible — reads
                                               # dst.yaml, so it runs BEFORE the
                                               # first apply
dst apply                                  # probes the connection before landing it
```

Introspect searches every non-system schema and writes out qualified names
(`spider.player`): one column per line, printing the value its `fields[].type`
takes, with the warehouse's own type in parentheses. `--profile` also samples
the data itself: the distinct values of code-like columns, null rates, and
ranges (row-capped reads, one pass per table in scope). In a
warehouse whose `status` holds `'A'/'C'/'X'`, those codes are the business
knowledge you are here to write down. Without `--profile` the listing is
schema only, and says so.
Add `--json` when something parses the output instead of reading it.

Write `semantic/entities/*.yaml` and `semantic/definitions/*.md` from the introspect
output (the scaffolded `.claude/skills/dst-semantic/` skill walks an agent through
it), select them in a lens's `lens.yaml`, then `dst plan` → `dst apply` →
`dst query` to verify. Details: [Connect a warehouse](guides/connect-a-warehouse.md)
and [Lenses](concepts/lens.md).

## 7. Let callers in

```bash
dst keys create --caller alex
```

One key per **person**: agents ask on a person's behalf, and knowing who asked
is the point. Access is deny-by-default: a caller can query a lens only if that
lens's `lens.yaml` has a matching entry:

```yaml
access:
  allow:
    - caller: alex        # or: - group: everyone   (any valid key in the org)
```

`dst apply` again, then prove the grant: ask *as* that caller, and check that an
ungranted one is still refused:

```bash
dst query customer_value "how many customers?" --key dst_alex...   # → the answer
dst query customer_value "how many customers?" --key dst_other...  # → exit 1, 403
```

`--key` is not a convenience: your admin token bypasses every allow-list, so without it
both of those return an answer and the grant is never actually tested.

Then connect an agent to the governed MCP door with the caller's key:

```bash
claude mcp add dst http://localhost:8000/mcp --transport http \
  --header "Authorization: Bearer dst_..."
```

The registration name is how people will invoke it in their AI ("check in dst
what our ARR is") — pick your own ("watson") and set `DST_INSTANCE_NAME` in
`.env` to match, so the server presents itself by the same name.

The agent gets the same governed pipeline as every other caller; see
[Agents over MCP](guides/agents-mcp.md), and the [API reference](reference/api.md) for
the REST and OpenAI-compatible doors.

Upgrading later? Run `dst migrate` after every `pip install -U`: `dst serve`
refuses a schema behind its build. See [Upgrading](upgrading.md).
