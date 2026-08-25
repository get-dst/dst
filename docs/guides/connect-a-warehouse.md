# Connect a warehouse

dst ships five warehouse connectors (`services/lenses/connections.py:98`):

| Type | Read-only mechanism | Runaway-query cap |
|---|---|---|
| `duckdb` | every query connection opened `read_only=True` | — (local file) |
| `postgres` | session forced `default_transaction_read_only=on` | `statement_timeout`, 30 s default |
| `mysql` | query session set `SESSION TRANSACTION READ ONLY` | `MAX_EXECUTION_TIME`, 30 s default |
| `bigquery` | read-scoped credential | `maximum_bytes_billed`, 10 GB default |
| `snowflake` | read-scoped role | `STATEMENT_TIMEOUT_IN_SECONDS`, 30 s default |

On `bigquery` and `snowflake` the read-only scope is the grant you give the service
account or role: dst passes the credential through, it does not downgrade it. On the
other three it sets the session itself. Timeouts and the bytes cap are per-connection
overridable (`statement_timeout_ms`, `max_bytes_billed`).

Read-only is layered, not assumed: the SQL guard is the first line — it rejects every
non-SELECT statement, including DML or DDL hidden inside a CTE, on the generated path
and the caller-SQL path alike (`services/runtime/sql_guard.py:297`) — and the
read-only credential/session is the backstop. There is no write path for callers.

## Declaring a connection

Connections are declared in `dst.yaml`, never created in the UI
(`services/project/schema.py:23`):

```yaml
connections:
  finance_wh:
    type: snowflake
    config: { account: acme-eu, warehouse: ANALYTICS_WH, database: FINANCE }
    secret_env: DST_API_KEY_FINANCE_WH
```

`config` holds the non-secret settings; the credential lives in `.env` under the env var
`secret_env` names (convention: `DST_API_KEY_<NAME>`). For BigQuery, point the env var
at the service-account JSON with the `@/path/to/key.json` idiom instead of pasting the
blob, and scope a wide project with `datasets: [finance_marts, product_marts]`: a
real layer usually reads several datasets, and the pin scopes introspection **and**
`dst probe` alike (`dataset:` and the generic `schema:` spelling are accepted too).
A config key the connector doesn't read warns at apply and on `dst introspect`
instead of sitting there silently. On `dst apply`, every new or changed declaration is
probed (connect plus one read) before it lands; an unchanged one skips the round-trip
and reports nothing, never a stale ✓ (`services/project/apply.py:203`). A dead
credential never replaces a working one, the error names the env ref to fix, and the
probed connection reports its capabilities on the apply output
(`read ✓ · query ✓ · query history ✗ (drift audits disabled: … — grant …)`), so a
missing grant is a visible degradation at deploy time, not a silent nightly skip.
See [Project files](project-files.md) for the full file model.

To try dst without a warehouse, `dst init --warehouse demo` uses a bundled DuckDB
fixture that ships inside the package.

## What dst needs from the warehouse

- **A read-only credential** that can `SELECT` the tables your lenses expose and read the
  catalog (schema introspection). Introspection reads only system views the grant already
  scopes: `information_schema` on Postgres/MySQL/Snowflake, the catalog API on BigQuery,
  `duckdb_tables()`/`duckdb_columns()` on DuckDB. On Postgres the catalog pass also reads
  `pg_catalog` (`pg_class`, `pg_attribute`, `pg_stats`, `pg_stat_all_tables`) — readable
  by any role, and `pg_stats` is already row-filtered to relations you can select, so
  the `SELECT` grant is the whole grant.
- **Sampling reads** during profiling: after a connection is created, a background chain
  profiles it: catalog, then value sampling, then column descriptions
  (`services/api/mgmt_connections.py`). Sampling runs through the connector's own
  read-only query path. The description step sends schema and sampled values to your
  configured LLM provider; [Security and data flow](../security.md) states exactly
  what leaves and how to turn it off (`DST_LLM_DESCRIPTIONS=false`).
- **Optionally, the history catalog** (Snowflake `ACCOUNT_USAGE`, BigQuery
  `INFORMATION_SCHEMA.JOBS`) to [bootstrap from history](drift-audit.md). The mining
  SQL reads statements and metadata only, never row data.

## What dst does not need

- **No write or DDL rights.** Grant none. Nothing in serving, profiling, evaluation or
  the drift audit issues DML or DDL, and the SQL guard refuses it before it could.
  dst has exactly one code path that writes to a warehouse: the write probe, which
  creates a throwaway table, inserts one row and drops it
  (`services/lenses/connection_eval.py:54`). It runs only when a connection is
  registered through the management API asking for `access: ["write"]`; a connection
  declared in `dst.yaml` never does — `dst apply` probes for read only
  (`services/project/apply.py:223`). With a read-only grant that probe simply fails,
  and nothing else in the product notices.
- **No agents or extensions installed in the warehouse.** dst connects like any
  read-only client, and creates no schema, table or view of its own.
- **No copy of your tables.** dst's own state (semantic assets, lens versions,
  traces, embeddings) lives in its own Postgres. Query results flow through to the
  caller; profiling stores sampled values and descriptions, not table extracts.

## Where the server runs

One process (API + dashboard) plus one Postgres. `dst dev` runs both on a laptop
(compose Postgres → migrate → serve on :8000); `deploy/docker-compose.yml` is the
self-host recipe: a pgvector Postgres and the app container, with idempotent migrations
on start. Postgres is the only stateful dependency — the one thing the container itself
writes to disk is the `local` embedding provider's model cache, and only when you
configure that provider (`services/context/local_embedder.py:29`).

