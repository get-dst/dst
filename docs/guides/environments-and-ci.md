# Environments and CI

There is no `--env` flag. An environment in dst is **a server plus a token**:
`dst apply` sends your project directory to whichever server the URL resolves to,
authenticating as whichever org the token belongs to. Dev, staging and production
are the same project files aimed at different addresses.

That is the whole model, and everything below is how to hold it: which address a
command picked, how to run the pipeline that publishes to it, and what stays
different between environments when the files are identical.

## Pick a shape first

| Shape | What separates | Use when |
|---|---|---|
| **Server per environment** | container, Postgres, provider keys, warehouse credentials | production matters — a staging apply can never touch prod's data or its stored credentials |
| **Org per environment, one server** | orgs are row-level-security isolated: lenses, callers, traces, certified answers | you want a rehearsal target cheaply; a staging org is one `dst bootstrap`, not one deployment |

Org-per-environment is real isolation of *content*, not of *infrastructure*: both
orgs share the container, the database and the provider budget, and an upgrade
migrates both at once. Rehearse migrations somewhere else.

## Which server did that command talk to

Resolution order, for every remote command (`services/cli/main.py:1540`):

1. `--url` on the command
2. `DST_URL` in the process environment
3. `DST_URL` in the **`--dir` project's `.env`** — not the shell's current directory
4. the built-in `http://localhost:8000`

Only the fourth is dst guessing, so it says so on stderr (`note: targeting … —
the built-in default: …`), and any failure to reach the server names the URL
*and* where that URL came from. A command never silently publishes to a server
you did not mean.

Tokens follow the same path: `--token` / `DST_ADMIN_TOKEN` for admin commands,
`--key` / `DST_API_KEY` for caller commands like `dst query`. The `--dir` hop is the
one to internalise: `dst apply --dir /repos/analytics` run from anywhere
authenticates as *that* project, and will **not** borrow the shell's token when
that project defines none. CI jobs and cron get this right by construction.

```bash
# per command
dst apply --dir . --url https://dst.prod.example.com --token "$PROD_ADMIN_TOKEN"

# per job — the usual CI form
export DST_URL=https://dst.staging.example.com
export DST_ADMIN_TOKEN=…
dst apply --dir .
```

## One project, many environments

The project directory is the source of truth, so there is nothing to promote:
you **apply the same git ref to the next server**. Staging applied commit `abc123`
and its gates went green; production applies `abc123` and gets a byte-identical
publish. Nothing is copied server-to-server, so nothing can drift in transit.

What legitimately differs between environments is the *physical address of the
data*, and that address belongs to the connection, not to the model. Entities
name a connection and a table ([project files](project-files.md)); the connection
declaration carries the catalog above it — BigQuery's `project`, Snowflake's
`database` — along with the credential.

```yaml
# dst.yaml
connections:
  warehouse:
    type: bigquery
    config: { project: acme-analytics-prod, dataset: marts }
    secret_env: DST_API_KEY_WAREHOUSE   # value lives in .env, never in the file
```

```yaml
# semantic/entities/orders.yaml — portable: no project, no environment
source: { connection: warehouse, table: marts.orders }
```

Compile stamps the connection's catalog onto a connection-relative table, so
that entity lands as `acme-analytics-prod.marts.orders` in production and
`acme-analytics-staging.marts.orders` in staging, from **one unedited file**. The
qualified name is what the generated SQL, the read allow-list, profile binding
and drift all see; the authored file stays portable. Two rules make it
predictable:

- **A table that carries its own catalog is never touched.** `raw-vendor.stripe.charges`
  means that vendor project in every environment — explicit always wins.
- **A bare table name is never qualified either.** What `orders` is missing is
  its schema, and a catalog cannot stand in for one.

Secrets were already environment-shaped: the file names an env var, each
environment supplies a different value. Non-secret `config` values are literal,
so the connection block itself is what differs per environment — keep an
environment-specific `dst.yaml` (or generate it in CI from a template) and leave
`semantic/` and `lenses/` byte-identical. Those are the files the gates run
against, and they are the ones that must never diverge.

!!! clarify "Qualify only as far as the connection doesn't"
    If a connection leaves `project:` out of its config and lets the
    service-account JSON pick one, dst does not know the catalog and qualifies
    nothing — the authored name reaches the warehouse as written. That is not a
    failure, it is the honest default: a guessed catalog is worse than a short
    name. Pin `project:` (or `database:`) in `dst.yaml` and entity files can drop
    it everywhere.

## The pipeline

Three jobs. On a pull request, prove the change; on merge, publish it; on a
schedule, catch what changed underneath you.

**On the PR — no server writes.** `dst plan` is the dry run against the target
server, and it changes nothing there: it validates every `semantic/**` file
through the same seam apply parses with, so a file apply would reject **exits 1
here**, on the PR, instead of at deploy time. A clean plan exits 0.

**On merge — one atomic publish.** `dst apply` runs as a single transaction under
a per-org lock: connections, then shared assets, then lenses, then a recompile of
anything left stale. Any error aborts everything and the previous versions keep
serving. `--require-gates` makes it fail closed when a lens configured for an
eval gate had that gate skipped (empty suite, provider error, unreachable
warehouse) — without it, a skipped gate publishes with a warning, which is not
what a deploy gate wants. `--quiet` keeps a large apply to the lines that need a
human.

**On a schedule — `dst test --all`** re-runs the certified corpus as a regression
suite, and `dst drift` compares the warehouse against the committed baseline.
Both are cheap to run nightly and are the only things that notice a table
changing shape under a published lens.

```yaml
# .github/workflows/dst.yml
name: dst
on:
  pull_request:
  push: { branches: [main] }

jobs:
  check:
    runs-on: ubuntu-latest
    env:
      DST_URL: ${{ vars.DST_STAGING_URL }}
      DST_ADMIN_TOKEN: ${{ secrets.DST_STAGING_ADMIN_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - run: pipx install dst-core
      - run: dst plan --dir .          # exits 1 if apply would reject a file

  publish:
    if: github.ref == 'refs/heads/main'
    needs: check
    runs-on: ubuntu-latest
    env:
      DST_URL: ${{ vars.DST_PROD_URL }}
      DST_ADMIN_TOKEN: ${{ secrets.DST_PROD_ADMIN_TOKEN }}
      DST_API_KEY_WAREHOUSE: ${{ secrets.DST_PROD_WAREHOUSE_KEY }}
    steps:
      - uses: actions/checkout@v4
      - run: pipx install dst-core
      - run: dst apply --dir . --require-gates --quiet
      - run: dst test --all            # exit 4 = nothing was verified: not green
```

A plan run on the PR needs a reachable server to diff against, which is why the
check job points at staging. There is no server-free file check: the parse that
rejects a bad file is the server's own, and running it anywhere else would be a
second implementation that drifts. If you would rather not expose a server to PR
builds, give the check job a staging server reachable only from CI — it holds no
production data and its admin token is scoped to it.

## Exit codes

Every command carries its outcome in the exit code, so a pipeline branches without
parsing prose.

| Verb | 0 | non-zero |
|---|---|---|
| `dst plan` | nothing would be rejected | `1` apply would reject a file |
| `dst apply` | published | non-zero on **any** error; nothing landed |
| `dst test` | everything verified passed | `1` diverged · `4` nothing was verified — treat as not-green |
| `dst query` | answered | `3` the lens *declined* (refusal or clarify) — a governed outcome, not a failure · `1` it broke |
| `dst doctor` | db, embedder and every model tier callable | `1` otherwise |

`dst test`'s exit 4 is the one to wire deliberately. It separates "the suite
passed" from "there was no suite", so a green light over an empty suite is
never mistaken for assurance.

## What a green pipeline still does not promise

`dst plan` prints its own `not checked by plan` block, and it is honest about
three things it cannot see without a live warehouse: connection probes,
eval-case `expected_sql` actually executing, and the publish eval gate. Those
happen at apply. A green plan means no file would be rejected — it is not a
promise that apply succeeds.

Two more environment-shaped facts worth pinning in your runbook:

- **Credentials are probed before they land.** Every warehouse declaration is
  connected to and read from during apply, so a dead credential in staging
  cannot replace a working one, and the error names the env ref to fix.
- **Migrations are per-environment and ordered.** Orchestrated deploys set
  `DST_MIGRATE_ON_START=false` and run `dst migrate` once per release, always as
  the same admin role. See [Deploying](../deployment.md).

## Secrets, per environment

- **`DST_SECRET_KEY`** belongs to the server, never to CI. It encrypts stored
  warehouse credentials; each environment has its own, and losing one orphans
  that environment's stored credentials.
- **`DST_ADMIN_TOKEN`** is per org, so per environment. Mint with
  `dst bootstrap`, which is idempotent — rerunning mints a fresh token without
  creating a duplicate org.
- **Warehouse credentials** reach CI only as the env vars your connections name
  in `secret_env`, and only in the job that applies to that environment.
