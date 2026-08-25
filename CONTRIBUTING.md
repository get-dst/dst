# Contributing

Thanks for looking at dst. Issues, docs fixes, and PRs are all welcome —
for anything larger than a bugfix, open an issue first so we can agree on the
shape before you invest in it.

## Dev setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (Postgres), Node 22 +
pnpm 11 (dashboard).

```bash
make install                # backend deps (uv sync)
make up && make migrate     # local pgvector Postgres + schema
make seed                   # dev org + admin token
make dev                    # API on :8000
pnpm -C apps/web install && pnpm -C apps/web dev   # dashboard on :5173
```

The [README](README.md) covers configuration (`DST_PROVIDERS` etc.); user
docs live in [docs/](docs/).

## Before you push

`make ci` must be green. It is the backend gate, with real exit codes:

- `ruff check` + `ruff format --check`
- `mypy services` — **strict, zero errors is the baseline**
- `pytest` (a local Postgres from `make up` is expected)
- `scripts/genuine_lint.py` — dashboard style rules
- `scripts/voice_lint.py` — shipped files, including the docs, must read as
  writing for a stranger: no tracker ids, no internal experiment names, no
  measurements of ours stated as product facts. It prints the file, line and
  reason for every hit.
- `scripts/gen_env_example.py --check` — `.env.example` is generated from
  `services/config.py`; add a setting and regenerate it in the same commit
  (`uv run python -m scripts.gen_env_example`).

If you touched `apps/web`, run the frontend checks too — CI runs them in a
separate job and `make ci` does not shell out to pnpm:

```bash
pnpm -C apps/web typecheck && pnpm -C apps/web lint && pnpm -C apps/web test
```

House rules worth knowing before a first PR:

- MCP tools in `services/mcp/server.py` must stay `async def` — a sync tool
  self-deadlocks the API under the remote transport (there's a regression test).
- Migrations are numbered sequentially in `migrations/versions/` — take the
  next free number.
- Shared contracts (`services/contracts/`) are seams other modules import —
  propose changes in an issue before editing them.

## Licensing of contributions

dst is [Apache-2.0](LICENSE). By submitting a contribution you agree it is
licensed under Apache-2.0 (inbound = outbound) and you certify the
[Developer Certificate of Origin](https://developercertificate.org/) — sign
your commits with `git commit -s`.
