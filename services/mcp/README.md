# dst MCP server

Exposes a caller's governed dst lenses as MCP tools, so an AI client
(Claude Desktop, Claude Code, Cursor) can discover and query them — grounded,
cited, scope-enforced, and audited, exactly like the REST data plane.

The tools follow the order an agent works in: find the lens, learn what it
means, reuse an approved answer if one fits, otherwise ask.

| Tool | What it does |
|------|--------------|
| `list_lenses` | The governed lenses this caller may use (scoped discovery). |
| `describe_lens(name)` | A lens's entities, typed fields, definitions, sample questions + certified library size. |
| `search_certified(lens, question)` | Find human-approved question→SQL pairs (scored, with provenance). |
| `run_certified(lens, cert_id \| question)` | Run an approved answer deterministically — zero AI SQL generation. |
| `query(name, question)` | Ask a governed question → cited answer + sql + rows + definition + confidence. |
| `send_for_review(request_id)` | Send a prior answer for verification; returns a review ticket. |
| `review_status(ticket_id)` | Poll a review ticket → its `state` + AI/human verdict (open → approved/changes/rejected). |

Every tool returns a structured envelope: `{"ok": true, ...payload}` on success,
`{"ok": false, "code", "error"}` on failure — so agents can branch instead of parsing prose.

**Bundled operating manual.** The server ships a cross-tool guide *with the connection*:
it's returned as the MCP `instructions` string in the `initialize` response, so any client
injects it as system context the moment dst connects (zero install, every transport).
It teaches what the tool docstrings can't — when to reach for dst, what a lens is,
deterministic-first, route-vs-query, and the review queue. An on-demand companion prompt,
`getting_started` (e.g. `/mcp__dst__getting_started` in Claude Code), kicks off a
governed-querying session. The live lens *catalog* deliberately stays behind `list_lenses`,
never baked into the manual.

Same tools, same governance, two transports:

- **Remote (recommended)** — mounted on the dst API at `/mcp` over
  streamable-HTTP. People connect with **OAuth** (just the URL — the client opens a
  browser, you sign in via your org's SSO, and the token is yours); headless service
  callers use a per-connection `Authorization: Bearer dst_…` key. Either way
  deny-by-default is enforced exactly as on the REST plane. **No repo, no `uv`, no admin
  token** — and `dstadm_…` admin tokens are *rejected* here.
- **Local stdio (dev)** — a thin process that proxies the REST API with a key from
  the environment. Useful when hacking on dst locally.

The lenses the client sees are exactly the ones the key is allowed to use
(deny-by-default). Every `query` is audited and full-trace logged.

> The easiest way to get a ready-to-paste config is the lens **Connect** modal (or
> **Settings → Callers**) in the dashboard: the human door is URL-only OAuth; the
> service doors issue a scoped key inline and fill in the snippet for you.

## Remote (recommended)

A deployed dst serves the MCP endpoint at `https://<your-dst>/mcp`. The URL needs
**no trailing slash** — `POST /mcp` is answered directly (not 307-redirected to `/mcp/`).
Two ways to authenticate; pick by *who* is connecting.

### OAuth — no key in the config

The config carries only the URL. On first connect the client gets a `401` +
`WWW-Authenticate`, discovers dst's authorization server, registers itself
dynamically, and opens a browser to a dst consent page where you **sign in** — when
Clerk is configured (the publishable key), that's a real browser sign-in (email / your
org's SSO), no keys involved. The client then receives its **own** `dsto_` token via PKCE,
bound to your caller identity, so every query is attributed. (Self-hosted instances with
no IdP fall back to authorizing the page with a `dst_`/`dstadm_` credential.) The flow is
fully server-side — no dashboard SPA required.

**Claude Code**

```bash
claude mcp add --transport http dst https://<your-dst>/mcp
```

**Claude Desktop / Cursor** — `claude_desktop_config.json` (or `~/.cursor/mcp.json`):

```jsonc
{ "mcpServers": { "dst": { "url": "https://<your-dst>/mcp" } } }
```

Restart the client, complete the browser sign-in, and you'll see a 🔌 tool indicator; ask
*"list my dst lenses"* then *"using the customer_value lens, how many repeat customers
are there?"*.

### Scoped key — for headless agents / CI / apps

A service caller that can't do a browser flow uses a scoped key (`dst_…`, issued in the
lens Connect modal or Settings → Callers) as a bearer header. **Never an admin token** —
`dstadm_…` is rejected on `/mcp` (it would bypass per-lens scoping and lose attribution).

**Claude Code**

```bash
claude mcp add --transport http dst https://<your-dst>/mcp \
  --header "Authorization: Bearer dst_your_caller_key"
```

**Claude Desktop / Cursor** — `claude_desktop_config.json` (or `~/.cursor/mcp.json`):

```jsonc
{
  "mcpServers": {
    "dst": {
      "url": "https://<your-dst>/mcp",
      "headers": { "Authorization": "Bearer dst_your_caller_key" }
    }
  }
}
```

## Local stdio (dev)

Runs the server from a local checkout; the key comes from `DST_API_KEY`.

**Claude Code**

```bash
claude mcp add dst \
  --env DST_API_KEY=dst_your_caller_key \
  --env DST_URL=http://localhost:8000 \
  -- uv run --directory /path/to/dst python -m services.mcp.server
```

**Claude Desktop / Cursor** — `claude_desktop_config.json` (or `~/.cursor/mcp.json`):

```jsonc
{
  "mcpServers": {
    "dst": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/dst",
        "python", "-m", "services.mcp.server"
      ],
      "env": {
        "DST_API_KEY": "dst_your_caller_key",
        "DST_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Naming the instance

`DST_INSTANCE_NAME=watson` (deployment env, or the project `.env` for stdio) makes the
server present itself as `watson` — FastMCP server name plus the operating manual it
ships as `instructions` — so an org that registers the client as `watson` gets a
self-description that matches ("check in watson what our ARR is"). Unset, it's `dst`.
`dst init` asks for the name and writes both ends.

## Run / debug manually

```bash
# stdio
DST_API_KEY=dst_… DST_URL=http://localhost:8000 \
  uv run python -m services.mcp.server
# or inspect the remote endpoint with the MCP Inspector:
npx @modelcontextprotocol/inspector
# → transport: "Streamable HTTP", URL: http://localhost:8000/mcp,
#   header: Authorization = Bearer dst_…
```
