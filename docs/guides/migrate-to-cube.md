# Migrate to Cube

From 0.15.0 a lens of a Cube-backed project serves through Cube: a question resolves
to a Cube query over the lens's views, dst checks it before and after execution, Cube
compiles it, and dst runs the compiled SQL through the lens's own connection and
signs the receipt. This guide moves an existing entity project (`semantic/entities/`,
`lenses/`) onto that lane. A new project starts there directly with `dst init --cube`
([Quickstart](../quickstart.md)).

Nothing here is forced. A project with no `cube/overlay.yaml` keeps serving every lens
on the typed core, exactly as before, with no file change.

## What decides the lane

Each lens serves on one of two lanes, `cube` or `typed`, decided in this order:

1. `DST_SERVING=typed|cube` in the server's environment, for every lens of the install;
2. the lens's own `serving: typed|cube` in `lens.yaml`;
3. otherwise `cube` when the lens is Cube-backed, and `typed` for every other lens.

A lens is Cube-backed when the push carries `cube/overlay.yaml` and either the overlay
lists the lens under `lenses:` or the lens's `lens.yaml` names `views:`. Without the
overlay no lens is Cube-backed, `views:` or not. Apply then checks
the lens against Cube's model and stamps the `model_version` its views compile to;
serving reads that stamp.

So the switch for a lens happens at the first `dst apply` that pushes an overlay
listing it. Writing the files changes nothing on the server. What stays where it is:
`semantic/` (migrate-model reads it and never deletes it), `lenses/<lens>/lens.yaml`,
certified answers and eval cases.

## 1. Preview the model

```bash
dst migrate-model --check
```

`--check` (alias `--dry-run`) writes nothing. It lists each file the run would create,
update or remove, prints a unified diff of each one that would change (a new file as
its size), and ends with the next-steps checklist from step 3 onward:

```
  + cube/MIGRATION.md
  + cube/model/customers.yml
  + cube/model/orders.yml
  + cube/model/views/customer_value.yml
  + cube/overlay.yaml
+++ cube/MIGRATION.md (new, 57 lines)
+++ cube/model/customers.yml (new, 42 lines)
…
Migration (check): 5 create; 3 item(s) carried outside Cube.
Next steps:
  [ ] 1. dst.yaml: a `cube:` block — …
```

Exit 2 when something would change, 0 when the model on disk is current, and 1 when a
generated file was edited by hand since it was written (the run refuses; see below).

## 2. Write the model

```bash
dst migrate-model
```

Flags: `--dir` (the project root, default `.`), `--out` (default `cube/` under it),
`--force`. It writes:

| File | What it holds |
|---|---|
| `cube/model/<entity>.yml` | one cube per entity: fields as dimensions, metrics as measures, a definition with `sql` as a segment |
| `cube/model/views/<lens>.yml` | a scope view per lens, plus one view per cube with in-lens joins |
| `cube/overlay.yaml` | what dst reads that no member's `meta.dst` carries: each lens's views and policy, each cube's clock, feeds and routing text, prose and ambiguous definitions, hide exceptions |
| `cube/MIGRATION.md` | every item the Cube model does not carry as authored, with the failure mode a question would meet there |
| `cube/cube.js` | the sidecar's config, written once when absent and never overwritten |
| `cube/.dst-migrate.json` | a sha256 per generated file, so a re-run knows what it wrote |

The [CLI reference](../reference/cli.md#dst-migrate-model) lists what changes shape on
the way (population filters, blueprint versions, text months, ratio metrics).

The output is generated once and then yours. A re-run rewrites only files it wrote and
nobody edited since, removes a generated file its source no longer produces, and never
touches a file it did not write. A generated file you edited is kept while its source
is unchanged; when the source has changed too, the run stops with exit 1 naming the
file, and `--force` overwrites it (the edit is lost).

`cube/cube.js` maps the groups in dst's token to Cube's `contextToGroups`, so a column
hidden with exception groups reaches those groups and no one else, and it attaches a
DuckDB file read-only. Keep it unless you replace both.

## 3. Point dst at Cube

Add a `cube:` block to `dst.yaml`:

```yaml
cube:
  url: http://localhost:4000
  api_secret_env: CUBEJS_API_SECRET
```

and the secret to `.env`. dst signs each request's token with it and Cube verifies it,
so any long random string works:

```bash
echo "CUBEJS_API_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
```

Every key of the block is in [Configuration](../reference/configuration.md#cube-cube).

## 4. Add and start the sidecar

Add this service under `services:` in the project's `docker-compose.yml`. It is the
Cube Core image this release is tested against, the same one `dst init --cube`
scaffolds:

```yaml
  # The pinned Cube Core sidecar: `docker compose --profile cube up -d`.
  cube:
    profiles: ["cube"]
    image: cubejs/cube:v1.6.75
    # List form on purpose: a bare entry is passed only when .env sets it; a
    # mapping with ${VAR:-} would hand Cube an EMPTY CUBEJS_DB_BQ_LOCATION (or
    # host, or path) and every query would fail while /readyz stays green.
    environment:
      - CUBEJS_DEV_MODE=false
      - CUBEJS_API_SECRET=${CUBEJS_API_SECRET:?set in .env}
      - CUBEJS_DB_TYPE=${CUBEJS_DB_TYPE:-duckdb}
      - CUBEJS_DB_HOST
      - CUBEJS_DB_PORT
      - CUBEJS_DB_NAME
      - CUBEJS_DB_USER
      - CUBEJS_DB_PASS
      # BigQuery: the service-account key as the container sees it (under /data)
      - CUBEJS_DB_BQ_PROJECT_ID
      - CUBEJS_DB_BQ_LOCATION
      - CUBEJS_DB_BQ_KEY_FILE
      # a DuckDB file as the container sees it (under /data): opened read-only
      - DST_DUCKDB_PATH
      - CUBEJS_CACHE_AND_QUEUE_DRIVER=memory
      - CUBEJS_SCHEDULED_REFRESH_DEFAULT=false
    ports:
      - "4000:4000"
    volumes:
      - ./cube/model:/cube/conf/model:ro
      - ./cube/cube.js:/cube/conf/cube.js:ro
      - ${DST_CUBE_DATA:-./cube/data}:/data:ro
    healthcheck:
      test: ["CMD", "node", "-e", "require('http').get('http://localhost:4000/readyz',r=>process.exit(r.statusCode==200?0:1)).on('error',()=>process.exit(1))"]
      interval: 5s
      timeout: 3s
      retries: 20
```

Cube reads the warehouse itself, with Cube's own settings, which compose fills from
`.env`:

- **DuckDB**: set `DST_CUBE_DATA` to the directory holding the file (default
  `./cube/data`) and `DST_DUCKDB_PATH` to the file as the container sees it, e.g.
  `/data/shop.duckdb`. `cube.js` attaches it read-only, so dst's own connection can
  open the same file beside it.
- **Any other warehouse**: set `CUBEJS_DB_TYPE` and the `CUBEJS_DB_*` values Cube's
  driver for it reads, and leave `DST_DUCKDB_PATH` unset.
- **BigQuery**: `dst init --warehouse bigquery --cube` writes `CUBEJS_DB_TYPE`,
  `CUBEJS_DB_BQ_PROJECT_ID`, `CUBEJS_DB_BQ_KEY_FILE` and `CUBEJS_DB_BQ_LOCATION` into
  `.env` (the location looked up from the dataset you name, else asked). Never leave a
  variable in `.env` with an empty value: compose passes `CUBEJS_DB_BQ_LOCATION=` as an
  empty location, and Cube's driver fails every query with it. Leave the line out instead.

`dst cube status` runs one tiny query through Cube (`limit 1` on the smallest cube), so a
Cube that compiles but cannot reach the warehouse reports `warehouse FAIL` here, before
`dst probe` or the monitor meet it.

Then start it and check that dst reaches it:

```bash
docker compose --profile cube up -d
dst cube status
```

`dst cube status` prints the URL, whether the secret is set, Cube's `readyz`, the
`model_version` and the member counts, and exits 1 when Cube does not answer or its
meta does not parse.

## 5. Read the migration report

`cube/MIGRATION.md` lists every item the Cube model does not carry as authored, each
with the failure mode a question would meet. Fix in the model or the overlay what you
want served through Cube; the rest is the list of questions the Cube lane will decline
or ask back on.

## 6. Probe through Cube

```bash
dst probe --cube
```

Reads value dictionaries and data ends through Cube, as a caller would ask, into
`profiles/cube.probe.json`. Commit it with the model.

## 7. Plan

```bash
dst plan
```

Plan checks the overlay against Cube's `/v1/meta` per lens (unknown views and members,
`meta.dst` keys, hidden members Cube still serves) and prints one line with the lane
of every lens in the push:

```
serving: customer_value → cube (Cube-backed), finance → typed (lens serving:)
```

The reason in parentheses is `DST_SERVING`, `lens serving:`, `Cube-backed` or
`default`. In `dst plan --json` it is the row
`{"scope": "serving", "lanes": {"<lens>": {"lane": "cube", "why": "Cube-backed"}}}`.

## 8. Apply

```bash
dst apply
```

Apply runs the same Cube checks, and an error aborts the whole apply, so nothing lands
against a model that does not hold what the overlay names. A Cube that cannot be
reached is one `provider_down` error and aborts it the same way. Each Cube-backed lens
is stamped with its `model_version` and serves through Cube from this apply on.

## 9. Monitor, then apply again

```bash
dst monitor
dst apply
```

[`dst monitor`](../reference/cli.md#dst-monitor) judges what the Cube probe read:
stale clocks, stopped feeds, rows dated after the clock, partial periods, dictionaries
the warehouse no longer matches. It writes `profiles/cube.freshness.json`, and the
apply after it carries the data ends and feeds into the answers. Until the monitor has
run, the data-end and feed checks skip on a Cube lane. Schedule it the way you
schedule `dst probe`: it exits 2 on findings and 1 when Cube or the project could not
be read.

## 10. Test

```bash
dst test
```

The certified answers and eval cases now run through the Cube lane. A certified entry
whose `certified_answers.yaml` row carries a Cube `query` runs that query through
every check; one without serves its SQL as before.

## Keep a lens on typed, or the whole install

To migrate the files and keep one lens on the typed core, set it in that lens's
`lens.yaml`:

```yaml
serving: typed
```

An explicit `serving:` always wins over the Cube-backed default, and `dst plan` shows it
as `typed (lens serving:)`. To run every lens of an install on one lane, whatever the
files say, set `DST_SERVING=typed` or `DST_SERVING=cube` in the server's environment
and restart it.

## Rolling back

Either of these puts lenses back on the typed core:

- `serving: typed` in a lens's `lens.yaml`, then `dst apply`;
- `DST_SERVING=typed` on the server, for every lens at once: a restart, no apply;
- remove `cube/overlay.yaml` from the project, then `dst apply`: every lens apply had
  stamped against Cube republishes without the stamp and serves typed. Dropping one
  lens from the overlay (and its `views:`) does the same for that lens.

The entity files never moved, so the typed lane has what it served from before.
`dst migrate-model` does not delete `semantic/`.

## When the Cube lane is missing something

`dst doctor` prints a `lanes` block, one line per published lens with its lane and why,
and under a Cube lane a warning for each missing dependency:

```
lanes
  acme/customer_value  cube (Cube-backed)
    ! monitor never run: the data-end and feed checks skip — `dst monitor`, then `dst apply`
  acme/finance         typed (lens serving:)
```

| Warning | What it costs |
|---|---|
| `cube not configured` | every door on the lens answers 503 naming Cube; set `DST_CUBE_URL` or `cube: {url: …}` in `dst.yaml` |
| `monitor never run` | the data-end and feed checks skip until `dst monitor` and an apply |
| `no chat tier` | the HTTP question door answers 503 for callers that are not in driver mode; MCP agents in [driver mode](agents-mcp.md#driver-mode) write their own Cube query and are served with no model |

These are warnings, never failures: doctor's exit code is unchanged. `/ready` carries
the same facts in its `serving` block, never as a readiness gate. While Cube is down,
a lens served through it answers 503 naming Cube, and every typed lens is unaffected.
The [failure modes](../reference/failure-modes.md) page lists the modes a Cube-lane
answer can carry.
