# dst dashboard

The console for governing and observing a dst deployment: lenses and their
versions, the review queue, certified answers, the audit ledger with per-call
cost, data sources, router behaviour, callers and keys.

It is not an authoring surface and not a query client. Lenses, definitions and
certified answers are files in a git repository, deployed with `dst apply`;
questions arrive from the AI your team uses, over MCP or the API. What the
dashboard adds is the view across everything that already happened, and the
human rulings — approving a review ticket, issuing a caller key — that belong
to a person rather than a file.

## Develop

```bash
pnpm install
pnpm dev          # http://localhost:5173
```

The dev server proxies `/mgmt`, `/auth`, `/v1`, `/health`, `/ready` and `/mcp`
to `http://localhost:8000`, so every fetch in the app is a relative path and
there is no build-time API URL to get wrong. Point `DST_DEV_API` elsewhere to
develop against another instance. Run the API first (`uv run dst dev` from the
repo root, or `make dev`).

`pnpm test` (vitest), `pnpm lint`, `pnpm typecheck`, `pnpm build`. Node 22+.

## Production

`pnpm build` emits `dist/`, and the wheel build hook (`hatch_build.py`) copies
it into `services/web_dist`, so the released package serves the dashboard from
the API's own origin — no separate host, no CORS, no second deployment. A wheel
built without a prior `pnpm build` is API-only and says so at build time and at
`dst serve`.

## Design

Warm-paper neutrals, ink-only, with colour reserved for meaning: green
verified, red error, amber warning. The tokens are the source of truth, in
`src/index.css` — read the comments there before adding a colour. The
mechanical half of the rule is enforced by `scripts/genuine_lint.py` in the
repo root, which `make ci` runs against these files.
