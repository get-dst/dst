# Deploying

dst deploys as **one stateless container** (API + dashboard, same origin)
against **one Postgres with pgvector** that you provide. All state lives in
Postgres: lenses, curated context, certified answers, credentials (encrypted), traces;
the container keeps no state on disk (it wants a writable `/tmp` for Python scratch,
and a `local-embed` build caches model weights under `$HOME`). So the whole deployment
statement is:

> **pinned image tag + database URLs + `DST_SECRET_KEY` + `DST_PUBLIC_BASE_URL`**

Anything that can run that container against that database works: a VM with
compose, Kubernetes with the helm chart, Cloud Run. Images publish to GHCR on
every release tag; pin one, never `latest`.

Publishing a change is a CI/CD pipeline, not a console session — and the prior
version serves untouched until the atomic swap
([Environments and CI](guides/environments-and-ci.md) is that pipeline, end to end):

[![Publishing a refreshed lens: a definition changes, CI runs dst plan and dst test with real exit codes on the PR, apply is one transaction through three gates, and lens v7 serves until the atomic swap to v8](assets/figures/fig4-authoring.svg)](assets/figures/fig4-authoring.svg)

## The contract

In `DST_ENVIRONMENT=production` the server **fails startup by name** on a missing
`DST_SECRET_KEY` or `DST_PUBLIC_BASE_URL` — and on the well-known dev database
password — while DSNs without an `sslmode` get `sslmode=require` appended unless they
target a unix socket (libpq's default would silently connect unencrypted).

| Env var | Required (prod) | What |
|---|---|---|
| `DATABASE_URL` | yes | app DSN: the **non-superuser** `dst_app` role, so row-level security is enforced. Never a superuser: superusers bypass RLS, which silently disables tenant isolation |
| `DATABASE_ADMIN_URL` | yes | privileged DSN: `dst migrate`, and at runtime admin-token auth and OAuth run on the admin engine |
| `DST_SECRET_KEY` | yes | Fernet key(s) encrypting stored warehouse credentials + OAuth code signing, **comma-separated** (first encrypts, all decrypt). Generate with `dst secret`. **Losing it orphans every stored credential**; to change it, follow the rotation sequence below rather than swapping it. Unset, multi-instance MCP OAuth breaks nondeterministically |
| `DST_PUBLIC_BASE_URL` | yes | the origin you serve at (`https://dst.example.com`). OAuth metadata and review links derive from it instead of trusting forwarded Host headers, and the MCP transport allowlists its hostname alongside the localhost forms (any other Host gets a 421) |
| `DST_ENVIRONMENT` | yes | `production` switches the contract on (and drops dev localhost CORS origins) |
| `DST_PROVIDERS` | no | LLM/embedding providers, JSON; see [Configuration](reference/configuration.md) |
| `PORT` | no | listen port (default 8000); the container binds `0.0.0.0` |
| `DST_CORS_ORIGINS` | no | comma-separated extra origins, only for split-origin frontends |
| `DST_MIGRATE_ON_START` | no | default `true`: entrypoint waits for the DB and migrates before serving (right for compose). Orchestrated deploys set `false` and run `dst migrate` once per release |
| `DST_DB_POOL_SIZE` / `DST_DB_MAX_OVERFLOW` / `DST_DB_POOL_RECYCLE` | no | per-engine pool knobs (defaults 5/10/1800s). Budget `instances × 2 engines × (pool_size + max_overflow)` under your Postgres `max_connections` |

## Postgres requirements

- **pgvector ≥ 0.5.0** (the schema uses HNSW indexes): AWS RDS needs PG
  **15.5+/16.1+**, Cloud SQL PG 15+; Neon and Supabase ship it built in. The
  first migration runs `CREATE EXTENSION IF NOT EXISTS vector`.
- Migrations create the `dst_app` role and its grants (`0001`). **Always run
  migrations as the same admin role**: `ALTER DEFAULT PRIVILEGES` binds to the
  role that executed it, so switching admin users mid-history silently drops
  grants on new tables.
- `dst migrate` is idempotent and takes a blocking advisory lock: concurrent
  runs serialize instead of racing.
- Back up before upgrading, like any schema-owning app.

## VM: docker compose

[`deploy/docker-compose.yml`](https://github.com/get-dst/dst/blob/main/deploy/docker-compose.yml)
is a supported production path for single-machine deployments, not just a demo:

```bash
POSTGRES_PASSWORD=… DST_APP_DB_PASSWORD=… DST_SECRET_KEY=$(dst secret) \
  docker compose -f deploy/docker-compose.yml up -d
```

All three are required: compose refuses to start without them, and `dst migrate`
applies `DST_APP_DB_PASSWORD` to the `dst_app` role (which ships with no password)
on every start, so changing it here rotates it.

It runs pgvector Postgres + the app with migrate-on-start. The app service **builds
from this checkout**; to run a published release instead, replace its `build:` with
`image: ghcr.io/get-dst/dst:<tag>`. Put a TLS-terminating proxy (Caddy, nginx) in
front, and add `DST_PUBLIC_BASE_URL` + `DST_ENVIRONMENT=production` to the app
service's `environment:`: only the keys listed there reach the container.

## Kubernetes: helm

The chart is deliberately small: one Deployment, a Service, an optional Ingress,
and a pre-install/pre-upgrade Job that runs `dst migrate`. It never bundles
a database: point it at your managed Postgres.

```bash
kubectl create secret generic dst \
  --from-literal=database-url='postgresql+psycopg://dst_app:…@…/dst?sslmode=require' \
  --from-literal=database-admin-url='postgresql+psycopg://admin:…@…/dst?sslmode=require' \
  --from-literal=secret-key="$(dst secret)"

helm install dst oci://ghcr.io/get-dst/charts/dst --version <X.Y.Z> \
  --set publicBaseUrl=https://dst.example.com
```

Serving pods run with `DST_MIGRATE_ON_START=false` (migrations belong to
the hook Job) but still carry both DSNs: admin-token auth and
OAuth run on the admin engine. Replicas scale horizontally: pods are
stateless (with the Cube sidecar on, see [the limit below](#helm)).

## The Cube sidecar

A lens served through Cube needs a reachable Cube Core
([Migrate to Cube](guides/migrate-to-cube.md)). Both shapes below run the image tag
this release is tested against, pinned in
[`deploy/cube.version`](https://github.com/get-dst/dst/blob/main/deploy/cube.version)
(`v1.6.75`). An install with no Cube configured serves every lens that does not need
one, and says `not configured` wherever it reports on Cube.

Cube compiles the SQL; dst runs it through the lens's own connection, so the warehouse
credential Cube holds is used for `/v1/meta` and for `dst probe --cube` and
`dst monitor`, which read through Cube's `/v1/load`.

### Compose

`deploy/docker-compose.yml` carries the sidecar behind the profile `cube`:

```bash
COMPOSE_PROFILES=cube DST_CUBE_URL=http://cube:4000 CUBEJS_API_SECRET=$(dst secret) \
  DST_CUBE_MODEL=../cube/model \
  POSTGRES_PASSWORD=… DST_APP_DB_PASSWORD=… DST_SECRET_KEY=$(dst secret) \
  docker compose -f deploy/docker-compose.yml up -d
```

| Variable | What |
|---|---|
| `COMPOSE_PROFILES=cube` | starts the `cube` service; without it the app starts alone |
| `DST_CUBE_URL` | where the app reaches Cube: `http://cube:4000` on the compose network. Unset, no Cube is configured |
| `CUBEJS_API_SECRET` | the key dst signs each request's token with and Cube verifies; both services read the same value, and Cube refuses to start without one |
| `DST_CUBE_MODEL` | the project's `cube/model`, mounted read-only at `/cube/conf/model` (default `./cube/model`) |
| `CUBE_TAG` | the image tag, default the pinned one; bump it together with `deploy/cube.version` |
| `CUBEJS_DB_TYPE`, `CUBEJS_DB_HOST`, `CUBEJS_DB_PORT`, `CUBEJS_DB_NAME`, `CUBEJS_DB_USER`, `CUBEJS_DB_PASS` | Cube's own warehouse settings (type default `duckdb`) |
| `DST_DUCKDB_PATH` | a DuckDB file as the container sees it, under `/data`; `cube.js` attaches it read-only. Leave unset for any other warehouse |
| `DST_CUBE_DATA` | the host directory mounted read-only at `/data` (default `./cube/data`) |
| `CUBEJS_LOG_LEVEL` | Cube's log level, default `warn` |

The app waits for the sidecar's health check when the profile is on. The cache and
queue run in memory and scheduled refresh is off: this shape has no Cube Store, so it
is one Cube per app.

### Helm

The chart's `cube:` values are off by default. Two ways to turn them on:

- **A sidecar in the dst pod**: `cube.enabled: true` and the model from
  `cube.model.configMap` (flat, one key per file, views included: `kubectl create
  configmap dst-cube-model --from-file=cube/model --from-file=cube/model/views`) or
  `cube.model.existingClaim` (a PersistentVolumeClaim). The model mounts read-only at
  `/cube/conf/model`, and the chart ships `cube.js` as a ConfigMap. The app reaches
  Cube at `http://localhost:4000`. With no model source set, the chart fails to render.
- **A Cube you run**: `cube.external.url`. The chart adds no container and points
  `DST_CUBE_URL` there.

Either way `CUBEJS_API_SECRET` comes from one Secret both containers read,
`cube.secret.name` (default `dst-cube`) under `cube.secret.key` (default `api-secret`):

```bash
kubectl create secret generic dst-cube --from-literal=api-secret="$(dst secret)"
helm install dst oci://ghcr.io/get-dst/charts/dst --version <X.Y.Z> \
  --set publicBaseUrl=https://dst.example.com \
  --set cube.enabled=true --set cube.model.configMap=dst-cube-model
```

Cube's warehouse settings go in `cube.env` / `cube.envFrom`; a DuckDB file takes
`DST_DUCKDB_PATH` there and a mount through `cube.extraVolumes` /
`cube.extraVolumeMounts`. `cube.image` and `cube.resources` set the rest. The sidecar
has no shared cache, so the chart refuses `replicaCount` above 1 with `cube.enabled`;
to scale out, run Cube yourself and set `cube.external.url`.

### BigQuery

Cube's BigQuery driver authenticates with a service-account key. Give it a key
with `roles/bigquery.dataViewer` on the datasets the model reads and
`roles/bigquery.jobUser` on the project, and mount the key file read-only:

| Variable | Value |
| --- | --- |
| `CUBEJS_DB_TYPE` | `bigquery` |
| `CUBEJS_DB_BQ_PROJECT_ID` | the project the jobs run in; an unqualified `dataset.table` in the model resolves here |
| `CUBEJS_DB_BQ_LOCATION` | the datasets' location, e.g. `EU` or `US` |
| `CUBEJS_DB_BQ_KEY_FILE` | the key as the container sees it, e.g. `/data/bigquery-key.json` |

With compose, put the key in the directory `DST_CUBE_DATA` names (mounted at
`/data`, read-only):

```bash
COMPOSE_PROFILES=cube DST_CUBE_URL=http://cube:4000 CUBEJS_API_SECRET=$(dst secret) \
  DST_CUBE_MODEL=../cube/model DST_CUBE_DATA=/srv/cube-keys \
  CUBEJS_DB_TYPE=bigquery CUBEJS_DB_BQ_PROJECT_ID=my-project CUBEJS_DB_BQ_LOCATION=EU \
  CUBEJS_DB_BQ_KEY_FILE=/data/bigquery-key.json … docker compose up -d
```

With helm, set the same variables under `cube.env` and mount the key from a
Secret with `cube.extraVolumes` and `cube.extraVolumeMounts`.

A variable that is unset is not passed: the compose file lists Cube's warehouse
variables as bare entries, and the chart skips an empty `cube.env` value. An empty
`CUBEJS_DB_BQ_LOCATION` is not "unset" to Cube's driver: every query fails on it while
`/readyz` and the meta stay green. `dst cube status` runs one `limit 1` query through
Cube and reports the warehouse's answer, so this shows before `dst probe` does.

Three things differ from a DuckDB or Postgres warehouse:

- **Time dimensions are TIMESTAMPs.** Cube bounds a window with `TIMESTAMP(?)`,
  and BigQuery compares neither a `DATE` nor a `DATETIME` with a `TIMESTAMP`.
  `dst migrate-model` writes a time dimension over a `DATE` or `DATETIME`
  column (and a text `YYYY-MM` month) as `TIMESTAMP(...)` when the project's
  connection is BigQuery. A column the probe types `TIMESTAMP` stays bare, so a
  filter on a partitioning column still prunes. A model written by hand needs
  the same cast, or the query fails on the warehouse with the error named.
- **Windows are UTC.** Cube compiles its bounds in UTC
  (`'2026-08-01T00:00:00.000000Z'`) and BigQuery has no session time zone, so
  the same statement reads the same rows from any host. A `DATE` or `DATETIME`
  cast with `TIMESTAMP()` is read as UTC.
- **Queue driver.** Without Cube Store, set
  `CUBEJS_CACHE_AND_QUEUE_DRIVER=memory` (compose and the chart already do);
  otherwise Cube refuses every `/v1/load`, and `dst probe --cube` and
  `dst monitor` read nothing.

dst's own BigQuery connection (`type: bigquery` in `dst.yaml`, its key in the
connection's secret) runs the compiled statements, each capped by
`max_bytes_billed`. It can hold a different service account from Cube's.

### What reports on it

`/ready` carries a `cube` block (reachable, version, `model_version`, member counts, or
`not configured`) and a `serving` block: per published lens its `org`, `lens`, `lane`
and `why`, and for a Cube lane whether Cube is configured, when the monitor ran
(`monitor`) and whether a chat tier serves the HTTP question door (`chat_tier`).
Neither gates readiness: a lens served through Cube answers 503 naming Cube while it is
down, and every other lens is unaffected. `dst doctor` prints a `cube` row and a `lanes`
block with the same facts as warnings, and `dst cube status` checks Cube alone.

## Cloud Run and friends

Works, with three settings that matter:

- **min-instances = 1, CPU always allocated.** Post-response work (warehouse
  profiling after connection registration) runs in-process; scale-to-zero or
  CPU throttling silently kills it.
- **Migrations as a release step**: set `DST_MIGRATE_ON_START=false` and run
  `dst migrate` in a Cloud Run Job (or your deploy pipeline) per release.
- **Static egress** (Cloud NAT + VPC connector) if customers IP-allowlist their
  warehouses: Cloud Run's default egress IPs rotate.

Raise the request timeout if you trigger audits or eval suites over HTTP; they
run in-request.

## Backup & restore

The app container is stateless: **the database plus `DST_SECRET_KEY` are the
entire state**. Embeddings, traces, review queues, and
stored credentials all live in Postgres; there is no object store.

Back up two things, always together:

1. **The database.** `pg_dump --format=custom` of the one dst database
   (pgvector columns dump and restore like any other type; the restore target
   needs the extension available, same as [Postgres requirements](#postgres-requirements)).
2. **`DST_SECRET_KEY`**: in your secret manager, not beside the dump.
   Stored warehouse/context credentials in the dump are Fernet-encrypted with
   this exact key.

!!! warning "A dump without its key loses every stored credential"
    Restoring into a deployment with a **fresh** `DST_SECRET_KEY` produces an
    install whose stored warehouse and context credentials are permanently
    undecryptable. Restore with the original key, or plan to re-enter every
    credential. This no longer fails *silently*: the server checks the key against
    an encrypted sentinel row at startup and refuses to boot on a mismatch, so it
    is a failed deploy rather than a 503 on whichever connector is touched first.

**Rotating the key** on a live deployment is supported. `DST_SECRET_KEY` takes
a comma-separated list (the first key encrypts, all of them decrypt):

1. Deploy with `DST_SECRET_KEY=<new>,<old>`. Everything still decrypts.
2. Run `dst rotate-key`. It re-encrypts every stored secret under `<new>`,
   names any row it could not decrypt, and exits non-zero if any failed.
3. Once it exits 0, drop `<old>`.

Do not skip step 2, and do not drop the old key while step 2 is failing.

Restore = create the database, `pg_restore`, start the **same pinned image tag**
with the **same key**, and let `dst migrate` no-op as the version check.

## Upgrades

1. Back up Postgres (above).
2. Bump the pinned image tag (and chart version; they move together).
3. Run `dst migrate` (the compose entrypoint and helm hook do it for you).

Rate limiting is per-instance; at replicas > 1 treat limits as approximate.
Liveness is `/health`; `/ready` additionally exercises the database and the MCP
transport; point readiness probes at it with a generous period.
