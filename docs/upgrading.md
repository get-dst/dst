# Upgrading

One rule covers every upgrade path:

> **Migrate before you serve.** The database schema and the code ship together, and
> `dst serve` refuses to start against a schema behind its build.

The refusal is deliberate. A server running on an out-of-date schema answers questions
*correctly*, and loses every trace, silently, because `request_log` writes fail inside a
background task where no caller can see them (`services/db/schema_state.py`). The review
queue, the drift audit, `dst test` and `dst correct` are all views over
`request_log`, so that loss is the whole governance surface. One refused start costs one
command; the alternative costs an audit trail nobody knows is missing.

## pip

```bash
pip install -U dst-core
dst migrate
# then restart your server
```

`dst migrate` is idempotent and takes a blocking advisory lock, so concurrent runs
serialize instead of racing. It now says exactly what it did:

```
migrated 0037 → 0040 — 3 revisions applied (0038, 0039, 0040)
```

and on a database that was already current:

```
already at head (0040) — nothing to apply
```

A brand-new database reports `schema created at 0040` instead. The line is the
confirmation that pulling a new version actually needed migrating.

!!! note "`dst dev` migrates for you"

    `dst dev` is Postgres-up → migrate → serve in one command
    (`services/cli/serve.py`), so a local development loop never hits the refusal. It is
    only the `pip install -U` + `dst serve` path that needs the explicit step.

## Containers

Bump the pinned image tag (never `latest`) and let the entrypoint migrate:

```bash
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
```

`pull` only fetches an image the compose file names: the shipped
`deploy/docker-compose.yml` **builds** the app service from the checkout, so pin a
release by replacing its `build:` with `image: ghcr.io/get-dst/dst:<tag>` first.

`DST_MIGRATE_ON_START` defaults to `true`: the entrypoint waits for
`DATABASE_ADMIN_URL` to accept connections (up to two minutes), runs `dst migrate`,
then serves (`docker/entrypoint.sh`). That is the right shape for compose and
single-instance deploys.

Orchestrated deploys set `DST_MIGRATE_ON_START=false` and run `dst migrate` once
per release instead: helm does it in a pre-install/pre-upgrade Job, Cloud Run in a Job
or pipeline step. See [Deploying](deployment.md#upgrades).

!!! warning "The container path is not guarded by the CLI refusal"

    The entrypoint execs `uvicorn` directly rather than going through `dst serve`, so
    a container started with `DST_MIGRATE_ON_START=false` and no release-step
    migration **will** start on a behind-head schema. What catches it there is `/ready`:
    it reports `"status": "degraded"` with the schema state spelled out, and it is the
    only signal, so point readiness probes at `/ready` and not `/health`.

    ```json
    {"status":"degraded","db":"ok","schema":"BEHIND — the database is at 0037, this build needs 0040 (3 unapplied)", …}
    ```

## What the refusal looks like

Run `dst serve` against a schema that has not caught up and you get this on stderr,
exit code 1, with no server started:

```
error: the schema is behind this build — this database is at 0037, this build needs 0040 (3 unapplied migrations: 0038, 0039, 0040).
Serving anyway loses the audit trail in silence: answers are served correctly and get a request_id, but every request_log write fails in a background task where no caller can see it — and the review queue, drift audit, `dst test` and `dst correct` are all views over request_log.
Run `dst migrate` (or `dst dev`, which migrates and then serves), then start the server.
```

The fix is the last line: run `dst migrate`, then start the server again.

Two states that look similar are **not** refused, on purpose:

- **ahead**: the database carries a revision this build has never heard of. That is
  older code on a newer schema, which is the *safe* deployment order (expand the schema,
  then roll the code), so it starts normally.
- **unknown**: the question could not be asked, usually a database still coming up. A
  slow database is not a broken one.

## After upgrading, `plan` may print `!` lines

`dst plan` compares your files against the server, so the one thing it structurally
cannot see is a release that changes what an **unchanged** file *means*. Those changes
leave a one-line notice on exactly the lenses they moved, and `plan` renders it under the
lens, prefixed with `!`:

```
tox: unchanged
  ! generation temperature: this lens generates at 0.0 now and generated at 0.2 before this upgrade — `temperature: 0.0` in its config had no reader before this upgrade (answer_mode: balanced supplied 0.2) and is live now. Nothing to fix if 0.0 is what it meant; remove `model.temperature` from lens.yaml to go back to 0.2. Clears on the next apply.
```

A `!` line is **not** a diff and not an error: there is nothing in your files to fix, and
`plan` still exits 0. It is telling you that behavior moved underneath a file that did
not. Read it, decide whether the new interpretation is what the lens meant, and either
leave it or edit the file.

`dst apply` prints the same notice once as a warning (so an operator who applies
without planning first is not the one person it never reaches), and publishing **clears**
it (`services/lenses/store.py`). The notice therefore lives until its owner next applies,
and a project created after the change never sees it at all.

## After upgrading, refresh the scaffolded skills

`AGENTS.md` and `.claude/skills/` are written by `dst init` and then never touched
again: they are a snapshot of the release you scaffolded with. Improvements to the
authoring skills ship with the package, so a project created before them keeps the old
copies indefinitely, and nothing in `plan` or `apply` can tell you: they are your files,
not managed project files.

```bash
dst init . --skills-only
```

Rewrites exactly those files from the installed dst and reports each as unchanged,
updated (`+N -M`), or new. It touches nothing else, and the write is unconditional; a
local edit is replaced, and survives as a diff in your own git. Worth running as the last
step of every upgrade.

## 0.15.1

### Migrations

`dst migrate` applies two revisions:

| Revision | What it does | Downgrade |
|---|---|---|
| `0077` | adds `caller.idp_subject`: the identity-provider subject an MCP sign-in bound the caller to, unique per org | drops the column |
| `0078` | adds `request_log.lane`: `explore` on an outside-lens read (the `sql` tool inside a lens, an answer read from an exposed table), NULL on governed rows | drops the column |

### Questions that ask for no number are answered before routing

A definition, a capability ask, a SQL ask, a "what report shows", "my last request" and a
question not about data each come back under a new mode (`definition`, `capability`,
`sql_request`, `hand_off`, `admin`, `not_a_data_question`) instead of a clarification or an
`unroutable` decline ([Question types](concepts/question-types.md)). A client that switched on
`mode` for the twenty failure modes sees six more values. `dst observe` and `dst test` count
them apart. Declare `hand_off:` in `dst.yaml` so "what report shows" and "email this to"
asks have somewhere to point; without it they are refused saying so.

### Identity-provider groups need a mapping

The groups claim of an OIDC token used to pass through as dst group names. It now
reaches dst only through a mapping declared under `groups:` in `dst.yaml`
([Security](security.md#hidden-columns-and-group-exceptions)):

```yaml
groups:
  analysts:
    idp: [analysts]
```

A deployment whose lens allow-lists or `except_groups` name IdP groups directly loses
those grants on upgrade until the mapping is applied; nothing is granted that was not
before. The list form of `groups:` keeps working for admin-set groups.

## 0.15.0

### Migrations

Run `dst migrate` before serving, as for every release. It applies four revisions:

| Revision | What it does | Downgrade |
|---|---|---|
| `0073` | admits `blueprint` as a shared semantic asset kind, for table blueprints (`semantic/tables/*.yaml`) | deletes the blueprint rows, then narrows the check |
| `0074` | adds `request_log.mode` and `eval_result.mode`: the [failure mode](reference/failure-modes.md), one name per row; nullable, and rows written before stay as they are | drops both columns |
| `0075` | drops `patch_candidate`: server-side patch drafting is retired, and the review queue (tickets, rulings, certify-on-approve) stays | recreates the table empty; candidates are not preserved |
| `0076` | adds `caller.driver` (default `false`): a caller an admin marked a [driver](guides/agents-mcp.md#driver-mode) | drops the column |

### Which lenses change lane

A project with only entity files (`semantic/`, `lenses/`, no `cube/overlay.yaml`) keeps
serving every lens on the typed core, with no file change.

A lens's `serving:` key is now optional. Unset, a Cube-backed lens (the push carries
`cube/overlay.yaml`, and the overlay lists the lens or its `lens.yaml` names `views:`)
serves through Cube, where 0.14 served it typed. To keep such a lens on the old lane,
set `serving: typed` in its `lens.yaml` before the first apply on 0.15.0, or
`DST_SERVING=typed` for the whole install. `dst plan` prints the lane of every lens
and why; `dst doctor` and `/ready` print it per published lens. A push without
`cube/overlay.yaml` clears every Cube stamp on apply, so removing the overlay puts its
lenses back on the typed core. Moving an entity
project to Cube on purpose is [Migrate to Cube](guides/migrate-to-cube.md).

### Removed

- The `import` verb, with `import dbt` (the dbt importer) and the OSI import. Write
  `semantic/` from `dst introspect`, or a Cube model with `dst migrate-model`.
- `dst export osi`. `dst export` keeps one meaning: server lenses into this project.
- The benchmark runner.
- The corpus distiller and its endpoint, `POST /mgmt/lenses/{lens}/distill`.
- Server-side patch drafting, with the `patches` verb and its candidates. Corrections still
  open review tickets; the fix is drafted from the ticket and the files by the
  reviewer's own agent.

### Rolling back to 0.14.x

Downgrade the schema to `0072`, the 0.14 head, with the 0.15.0 package still installed
(its migrations carry the downgrades), then install the older package:

```bash
alembic downgrade 0072            # from the directory holding alembic.ini (below)
pip install 'dst-core<0.15'
```

Blueprints, failure-mode columns and driver flags are dropped on the way, and
`patch_candidate` comes back empty. 0.14 has no Cube lane and its lens schema has no
`serving:` or `views:` key, so take them out of `lens.yaml` before applying with it.

## Downgrading

### Rolling the code back

A stored lens bundle stays readable by the release that wrote it. Storage omits unset
keys rather than writing `null` (`services/lenses/store.py`), so an older build reading a
newer row falls back to its own defaults instead of raising on a type it does not expect.
That is what makes rolling a release back survivable: the previous version can still
read, serve, and `plan` the lenses the newer one published.

The guarantee is **one release back**, and it is a compatibility promise about payload
shape, not a general time machine. Do not assume a bundle written today loads under a
build from several releases ago.

### Rolling the schema back

Migrations carry downgrades, so the schema itself reverses with alembic, run from a
source checkout:

```bash
uv run alembic downgrade 0037
```

`script_location` is relative, so the command only resolves from the directory that
holds `alembic.ini` and `migrations/`. A `pip install` ships both beside the package:
`cd "$(python -c 'import services,pathlib;print(pathlib.Path(services.__file__).parents[1])')"`
first, then `alembic downgrade 0037`.

Some downgrades are deliberately no-ops because the data they would restore no longer
exists to restore: a migration that stripped null keys has nothing to put back, and one
that recomputed derived digests lets the next apply recompute them again. Reversing the
schema is therefore not always the same as reversing the data.

### When to restore instead

Reach for the backup rather than a downgrade when the problem is in the **payload** and
not the schema; that is the case `alembic downgrade` cannot help with, because the rows
themselves are shaped for the newer contract. Restore from `pg_dump` with the **same**
`DST_SECRET_KEY` the dump was taken under; a restore under a fresh key produces a
working-looking install whose stored warehouse credentials are permanently undecryptable.
See [Backup & restore](deployment.md#backup-restore).

Back up before every upgrade, like any schema-owning app. It is the only step on this
page that cannot be undone by another command.
