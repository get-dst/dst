# Build your own UI

Everything in dst is an HTTP endpoint. The bundled dashboard is a pure client of
the same public API you get: plain `fetch` with a bearer header
(`apps/web/src/api/client.ts`), no privileged channel, no server-side tricks. Whatever
it can do, your UI, script, or internal tool can do with the same calls.

## Credentials

Two tokens, two planes ([API reference](../reference/api.md)):

| Token | Minted by | Opens |
|---|---|---|
| `dstadm_` admin token | `dst bootstrap` (saved to `.env` as `DST_ADMIN_TOKEN`) | `/mgmt/*`: manage lenses, connections, reviews, keys |
| `dst_` caller key | `dst keys create --caller alex` (or `POST /mgmt/callers/{name}/keys`) | `/v1/*`: ask questions as a governed identity |

Both ride the same header: `Authorization: Bearer <token>`. An asking surface should
hold a caller key, not the admin token: the admin bypasses lens allow-lists, so
access bugs stay invisible until someone else hits them.

## The two calls a UI needs

What can this key see?

```bash
curl -s http://localhost:8000/v1/lenses \
  --header "Authorization: Bearer dst_..."
```

Ask:

```bash
curl -s http://localhost:8000/v1/lenses/customer_value/query \
  --header "Authorization: Bearer dst_..." \
  --header "Content-Type: application/json" \
  --data '{"q": "how many customers do we have?"}'
```

The response is an answer **with receipts** ([Receipts](../concepts/receipts.md)).
Render them, don't drop them:

- `answer`: the prose; `data`: the payload, with `columns`, `rows`, `row_count`.
- `truncated` (on the envelope, not inside `data`): `returned` and `total`, and `total`
  is `null` when the engine-side fetch cap bit and the true count is unknown.
- `sql`, `citations`: what actually ran and why.
- `confidence` (`verified` / `partial` / `unverified`): derived from named checks;
  `certification` (`certified` / `assisted` / `none`, plus `certified_failed` on an
  error status): whether a human approved this exact question→SQL pair.
- `data_as_of`: the freshness stamp.
- `request_id`: the handle for corrections (below).
- `clarification`: when set, this is a governed non-answer, not an error: render the
  choices it carries and re-ask ([Clarify & refusal](../concepts/clarify-and-refusal.md)).

If your UI phrases the reply itself, pass `"format": "structured"`: same SQL, same
receipts, minus the LLM call that writes the prose, which is most of the latency
(on certified answers, roughly two orders of magnitude).

## Close the loop from your UI

A wrong answer reported from your surface feeds the same review queue as everyone
else's:

```bash
curl -s http://localhost:8000/v1/reviews \
  --header "Authorization: Bearer dst_..." \
  --header "Content-Type: application/json" \
  --data '{"request_id": "<from the answer>"}'
```

It answers 201, and only for a request served to this same caller — a UI cannot file
against someone else's answer. `GET /v1/reviews` lists the tickets on this caller's own
requests, so your UI can show reporters what happened
([The correction loop](correction-loop.md)).

## Types without writing them

The server publishes its own contract: most routes declare a response model,
so the schema is real, not decorative:

- `http://localhost:8000/docs`: interactive, try-it-out, always current.
- `http://localhost:8000/openapi.json`, to generate a typed client:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o dst.d.ts
```

(or `openapi-python-client`, or any OpenAPI generator; nothing about the schema is
TypeScript-specific.)

## CORS for a separate origin

Served same-origin (`dst serve` mounts the dashboard bundle on the API's port),
no CORS is involved. A UI on its own origin declares itself:

```bash
DST_CORS_ORIGINS=https://ui.example.com
```

Comma-separated for several origins. Dev servers on `localhost:5173`/`3000` are
allowed automatically outside production.

## The UIs you don't have to build

- **Any OpenAI-compatible chat UI** already works: point it at
  `POST /v1/chat/completions` with a `dst_` key, set `model` to `dst/<lens>`.
  `stream: true` is accepted and returns a valid SSE stream, but composition is
  synchronous, so it arrives as one content delta then `[DONE]` — not token-by-token.
- **Agent surfaces** get the same powers over MCP: twelve tools, same governance
  ([Agents over MCP](agents-mcp.md)).

## The control plane is yours too

Every page of the bundled dashboard (lens editing, publish, connections, the review
queue, observability) is `/mgmt` endpoints under the admin token, enumerated with
descriptions at `/docs`. An admin UI, a CI check that polls `/mgmt/observe/kpis`, a
bot that rules on review tickets: all the same API. A test pins every route to a
docstring and the reference doc to the app, so what you read there is what is served.
