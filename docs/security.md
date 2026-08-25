# Security and data flow

dst is self-hosted: one container you run, one Postgres you provide, talking to
a warehouse you own and to model providers you declare. This page states exactly
what leaves that boundary, when, and how to stop or contain each flow. Every
claim names the code that implements it.

## What leaves your network

dst sends your data to exactly two kinds of external endpoint, both configured
by you: the warehouse connections and the LLM/embedding providers declared in
`dst.yaml` or `DST_PROVIDERS`. There is no dst-operated endpoint anywhere in
the product. Three optional integrations reach further — Clerk, OIDC discovery
and the GitHub context source — and each is inert until you configure it; they
carry identity and context, never warehouse rows. All of them are named below.

### Connection profiling (on by default)

When a warehouse connection is created or updated — and on an explicit catalog
refresh — a background chain profiles it: a catalog pass, a sampling pass, then
an LLM description pass that fills in missing table and column documentation
(`services/api/mgmt_connections.py`, `services/api/mgmt_profile.py`,
`services/lenses/profile_enrich_lm.py`). That last pass sends, per table, to the
configured LLM provider:

- the table name, and every column's name and type;
- for a column that has no description yet, up to 8 example values from the
  value dictionary the sampling pass collected — a dictionary is kept only for
  columns with at most 25 distinct values (`services/contracts/profile.py:21`);
- for a column that already has one, that description instead of the values, so
  the model fills gaps without rewriting them (`profile_enrich_lm._col_line`).

Nothing else rides that prompt: no null rates, no value ranges, no row counts,
no table comment. The sampling pass that feeds it talks only to the warehouse.
It transfers at most 10,000 rows per query, but its statistics query is an
aggregate, so a table at or below 1,000,000 rows — or one whose row count the
catalog does not know — is scanned in full to make the counts exact, and only
above that is it sampled (`services/contracts/profile.py:26,51`). On BigQuery a
sampling query whose dry-run estimate exceeds ~2 GB is refused rather than run.

This is **on by default**, because undocumented columns are the common case and
the descriptions materially improve answers. To turn it off, set
`DST_LLM_DESCRIPTIONS=false`: the catalog and sampling passes still run, but
they only ever talk to the warehouse itself, and no profiling data reaches a
model provider.

**dst does not classify personal data, and it does not redact.** There is no
column classifier, no name list, and no redaction layer: whatever is in scope
is what gets sampled, described, and served. That is a deliberate choice. A
partial classifier is worse than none, because it invites you to trust it.

The boundary is therefore the one you draw: expose only what you are willing to
send. In practice that means pointing lenses at marts and views built for this
purpose rather than at raw tables, and leaving personal-data columns out of
them. If a column must exist in the model but its values must not leave, keep
`DST_LLM_DESCRIPTIONS=false` so no profiling pass reads it, and note that its
values can still reach the provider through a query that projects it.

### Per-question serving

Answering a question makes model calls to the configured provider. Those calls
carry:

- the question itself;
- the lens's semantic model, serialized in full: table and column names, types,
  descriptions, metrics, dimensions, business definitions, and sample queries
  (`services/runtime/generator.py`), including the profile facts folded in
  before generation: enum value dictionaries, value ranges, null rates
  (`services/lenses/profile_enrich.py`);
- retrieved curated-context chunks and certified-definition pages
  (`services/runtime/assembly.py`);
- the generated SQL and the first result rows, sent to the answer-composer call
  that writes the prose — 200 rows by default, raisable per lens with
  `max_rows_to_compose` and hard-bounded by the 5000-row fetch cap
  (`services/contracts/lens_config.py:68`, `services/runtime/pipeline.py:56`);
- for certified-answer matching and routing, the question also goes to the
  configured embedding provider, and routing sends the question to the
  fast-tier model with a shortlist of candidate lenses described by name,
  declared description, and the authored terms that identify them: metric and
  entity names, definition terms, `use_when` phrasings, sample-question
  wordings. No physical table names, and no data
  (`services/runtime/assembly.py`, `services/router/profiles.py:37`).

Uploaded context files are chunked and embedded at ingest, so their text goes
to the configured embedding provider once, at upload time
(`services/context/ingest.py`).


### Nothing else

There is no telemetry. dst makes no usage reporting, no version or update
checks, no crash reporting, and no license pings; the dashboard's fonts are
vendored into the bundle, not fetched. The complete list of modules that open
outbound connections: the warehouse connectors, the LLM/embedding providers
above, the CLI and MCP clients talking to your own dst server, the `local`
embedding provider fetching its model weights on first use
(`services/context/local_embedder.py`), and three integrations that are inert
until you configure them — OIDC discovery against your IdP
(`services/auth/oidc.py`), the GitHub context source
(`services/connectors/github.py`), and Clerk.

Clerk is the one exception to the no-CDN rule, and it is opt-in: set a Clerk
publishable key and the server verifies tokens against Clerk's JWKS endpoint
(`services/auth/clerk.py:47`) while the dashboard and the MCP consent page load
`clerk-js` from Clerk's CDN (`services/api/oauth.py:264`). Leave it unset — the
default — and no dst page loads a script from a host you do not run.

## Where data rests

All dst state lives in the Postgres you operate; the container writes nothing
to disk except the `local` embedding provider's model cache, and only when that
provider is configured (`services/context/local_embedder.py:29`).

Every served request appends a `request_log` row: the question, the SQL, the
answer text, citations, verification results, token counts and cost
(`services/observability/logger.py`). That write happens off the response path,
so it can fail after an answer has already gone out; when it does, the loss is
logged CRITICAL and counted on `/ready` rather than swallowed
(`logger._record_write_failure`). A sample of result rows (the first 5) is
stored only when the lens sets `logging.log_samples: true`; the default is off,
and the stored sample is the served rows verbatim
(`services/runtime/pipeline.py`).

Stored table profiles keep the sampled value dictionaries and ranges in
Postgres; they are readable on admin surfaces only. Warehouse credentials are
encrypted at rest with `DST_SECRET_KEY` — MultiFernet over a comma-separated key
list, so `dst rotate-key` re-encrypts without a window where either key is
wrong (`services/security/crypto.py`). There is no plaintext fallback: with no
key set, storing a credential fails with 503 rather than degrading
(`services/api/mgmt_connections.py:70`), and under `DST_ENVIRONMENT=production`
the server refuses to start at all (`services/config.py:429`). No endpoint
returns a stored secret; the API reports only whether one is present
(`services/lenses/connection_store.py`).

## Personal data

dst has no personal-data control. It does not classify columns, does not scan
values, and does not redact anything from a prompt, a result row, or a stored
trace. If a column is in a lens's scope, its values can reach the configured
model provider and any caller allowed to ask.

That is a deliberate omission rather than a gap waiting on a release. A
classifier that recognises `email` but not `salary`, `henkilotunnus`, or a
free-text `notes` column offers assurance it cannot keep, and assurance you
cannot keep is worse than none: it moves the decision away from the person who
knows the data.

So the boundary is yours to draw, and there is exactly one place to draw it:
**what you expose.**

- Point lenses at marts and views built for serving, not at raw tables. A view
  that omits a column is a boundary dst cannot cross, because it never sees it.
- Grant the warehouse credential read access only to those objects. The lens
  allow-list governs who may ask; the database grant governs what is reachable
  at all, and only the second one holds if a lens is misconfigured.
- Keep `DST_LLM_DESCRIPTIONS=false` if you do not want profiling to read values
  at all. It stops the sampling and description passes; it does not stop a
  question whose SQL projects the column.
- Remember what is retained: `request_log` keeps the question, the SQL, the
  answer, and — only when a lens sets `logging.log_samples` — a row sample. It
  lives in your Postgres, under your retention policy.

Column-level exclusion may be reconsidered later as its own design. It is not
in the product today, and this page will say so until it is.

## Keeping everything in-network

Providers are wire shapes, not vendors. An `openai-compatible` entry pointing
`base_url` at an internal endpoint (Ollama, vLLM, a corporate gateway) keeps
every model call inside your network (`services/llm/openai_compat.py`), and the
`local` embedding provider runs in-process with no key at all
(`services/context/local_embedder.py`):

```yaml
providers:
  house:
    type: openai-compatible
    base_url: http://llm.internal:8000/v1
    api_key_env: DST_API_KEY_HOUSE
    fast_model: house-small
    smart_model: house-large
  embeddings:
    type: local
```

With that configuration, no model call crosses your network boundary: warehouse,
Postgres, generation, composition and embedding are all yours. Two things still
reach outside it, both avoidable: the `local` provider downloads its ONNX weights
on first use, so an air-gapped install wants that cache pre-warmed or baked into
the image; and Clerk, if you configure it, is contacted on every sign-in. Leave
Clerk unset and no third party sits in the request path.

## Reporting a vulnerability

Report privately to **security@dataservetool.com** with steps to reproduce;
see `SECURITY.md` in the repository for scope and response expectations. Do not
open a public issue for a security report.
