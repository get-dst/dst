# dst — agent working rules

FastAPI + Postgres/pgvector backend (`services/`), React dashboard (`apps/web/`),
docs site (`docs/` + `mkdocs.yml`). Commands: `make dev` (API :8000),
`pnpm -C apps/web dev` (:5173), `make test`, `make lint`, `make migrate` (runs
`dst migrate`; bare alembic does not sync the app role), `make up` (local Postgres).

## This repository is public-ready, always

Every tracked file here is published as-is on release: the release overlays this
tree onto the public repo whole, with no denylist. So:

- Commit only what a stranger may read. Plans, specs, strategy, experiments,
  benchmarks, blog drafts, marketing and customer or sandbox material live in the
  separate internal repo, never here, not even "temporarily".
- A blog post enters `docs/blog/posts/` in the commit that publishes it. A file
  with `draft: true` fails `make ci`.
- A new top-level entry (or a new `docs/` section) fails
  `tests/test_public_tree.py` until it is added to the allowlist there. Adding
  it is the moment to ask whether it is public.
- Comments and docs explain the code to a reader; they never cite internal
  tickets, work items, test organizations, runs or people.

## Concurrent agents: one writer per working tree

1. The main checkout has one interactive writer at a time. Every other agent
   works in its own git worktree on its own branch, then merges when green.
2. Stage surgically: `git diff <file>` before `git add <file>`; never
   `git add -A`. A file with changes you did not make is another writer's work.
3. Every commit leaves a tree where `uv run python -c "import services.app"`
   succeeds on a clean clone.
4. Expect main to move: pull/rebase before push; re-read files before editing.
5. Plain commit messages saying what changed and why.

## Gates

- `make ci` before every push (ruff check, ruff format --check, pytest with a
  database required, mypy fully clean, dashboard lint, `.env.example` sync).
  Real exit codes: never gate on piped output.
- `make ci-clean` before ending a session: clones committed HEAD and runs the
  suite from scratch.
- `tests/test_live_auth_e2e.py` is opt-in via `DST_TEST_LIVE_E2E=1` (needs
  Postgres, no API keys).

## Domain notes

- Shared contracts (`services/contracts/`) are the seams between subsystems:
  change them deliberately, never incidentally.
- MCP tools (`services/mcp/server.py`) must stay `async def`: under the remote
  transport they run on the API's own event loop and proxy back into the same
  server, so a blocking tool deadlocks the API (a regression test pins this).
- Migrations are numbered sequentially (`migrations/versions/00NN_*.py`); check
  for the next free number, parallel branches must not both claim it.
- The admin engine has no BYPASSRLS on managed Postgres (Cloud SQL, RDS, Neon
  admin roles are not superusers; only local docker is). Any admin-engine read
  of a FORCE-RLS table must set the org GUC first
  (`set_config('app.current_org', :o, true)`), resolving the org from a no-RLS
  table (api_key, local_session, org). `tests/test_rls_managed_pg.py` pins it.
- Nothing customer-specific in `services/`: that belongs in a project's own files.
