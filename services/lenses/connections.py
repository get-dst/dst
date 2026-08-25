"""Resolve a lens's connection name to a live `Connector`.

Resolution order:
  1. a DB-backed `connection` row for the org (encrypted creds) — the customer path;
  2. built-in fallbacks for dev/seed: local `jaffle` DuckDB and an env `bigquery`.

Pass ``session`` to resolve on the CALLER's transaction: apply stages its
connections there (blue/green — nothing commits until the whole push succeeds),
so anything opening its own session is blind to a connection arriving in the
same push.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from services.config import settings
from services.connectors.bigquery import BigQueryConnector
from services.connectors.duckdb import DuckDBConnector
from services.connectors.mysql import MySQLConnector
from services.connectors.postgres import PostgresConnector
from services.connectors.snowflake import SnowflakeConnector
from services.contracts.protocols import Connector
from services.db.session import org_session
from services.lenses import connection_store, credential_resolver

if TYPE_CHECKING:
    from services.governance.credentials import CallerIdentity


class ConnectionUnavailable(ValueError):
    """No live `Connector` can be built for this connection name — it is not
    declared/applied in this org, or its env-backed fallback has no credentials.
    A ValueError so the `except ValueError` call sites keep their behaviour; its
    own type so the publish gate can degrade to a loud skip instead of a 500."""


# Every config key each connector actually reads — the source for
# `config_warnings`. An unknown key is accepted silently by the pydantic layer
# (`config` is a free dict), so a key like `schema: finance_marts` can sit in a
# dst.yaml doing nothing across repeated profiling attempts.
KNOWN_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "duckdb": frozenset({"path", "schema"}),
    "bigquery": frozenset({"project", "dataset", "datasets", "schema", "max_bytes_billed"}),
    "postgres": frozenset(
        {"host", "port", "database", "user", "schema", "sslmode", "statement_timeout_ms"}
    ),
    "mysql": frozenset({"host", "port", "database", "user", "statement_timeout_ms"}),
    "snowflake": frozenset(
        {
            "account",
            "user",
            "warehouse",
            "database",
            "schema",
            "role",
            "auth",
            "private_key_passphrase",
        }
    ),
}


def config_warnings(type_: str, config: dict[str, Any] | None) -> list[str]:
    """Unrecognized config keys for a known connection type — each one a warning.

    Types without a registry entry (context sources, plugins) warn on nothing:
    a false positive here would train people to ignore the channel."""
    known = KNOWN_CONFIG_KEYS.get(type_)
    if known is None or not config:
        return []
    return [
        f"connection config key '{key}' is not read by the {type_} connector "
        f"(it reads: {', '.join(sorted(known))}) — a misspelled key silently changes nothing"
        for key in sorted(config)
        if key not in known
    ]


def _bigquery_datasets(cfg: dict[str, Any]) -> list[str]:
    """Dataset pins from any of the three accepted spellings.

    `datasets: [a, b]` is the primary (a real layer reads several); `dataset:`
    stays for back-compat; `schema:` is what the generic connection reference
    documents, and a BigQuery connector that never read it would silently profile
    nothing."""
    plural = cfg.get("datasets")
    if isinstance(plural, list):
        return [str(d) for d in plural if d]
    single = cfg.get("dataset") or cfg.get("schema")
    return [str(single)] if single else []


def build_connector(type_: str, config: dict[str, Any], secret: str | None) -> Connector:
    """Build a live `Connector` from raw (type, config, secret) — no DB round-trip.

    Used both by `_build_from_record` (stored connections) and by connection
    evaluation at create time (before the connection is persisted).
    """
    cfg = config or {}
    if type_ == "duckdb":
        schema = cfg.get("schema")
        return DuckDBConnector(
            str(cfg.get("path") or settings.duckdb_jaffle_path),
            schema=str(schema) if schema else None,
        )
    if type_ == "bigquery":
        if not secret:
            raise ValueError("bigquery connection has no stored credentials")
        info = json.loads(secret)
        return BigQueryConnector.from_info(
            info,
            project=cfg.get("project"),
            datasets=_bigquery_datasets(cfg),
            max_bytes_billed=int(cfg.get("max_bytes_billed", settings.bigquery_max_bytes_billed)),
        )
    if type_ == "postgres":
        return PostgresConnector.from_record(cfg, secret)
    if type_ == "mysql":
        return MySQLConnector.from_record(cfg, secret)
    if type_ == "snowflake":
        return SnowflakeConnector.from_record(cfg, secret)
    raise ValueError(f"unknown connection type '{type_}'")


def _build_from_record(rec: connection_store.ConnectionRecord, secret: str | None) -> Connector:
    return build_connector(rec.type, rec.config or {}, secret)


def _builtin(connection: str) -> Connector:
    if connection == "jaffle":
        return DuckDBConnector(settings.duckdb_jaffle_path)
    if connection == "bigquery":
        if not settings.gcp_credentials:
            raise ConnectionUnavailable(
                "no connection 'bigquery' is registered in this org — declare it in "
                "dst.yaml (type: bigquery, secret_env: DST_API_KEY_BIGQUERY, "
                "with an @path service-account ref in .env) and run `dst apply`"
            )
        return BigQueryConnector(
            settings.gcp_credentials,
            project=settings.gcp_project,
            dataset=settings.bigquery_dataset,
            max_bytes_billed=settings.bigquery_max_bytes_billed,
        )
    raise ConnectionUnavailable(
        f"unknown connection '{connection}' — declare it in dst.yaml and run `dst apply`"
    )


def _from_session(
    session: Session, connection: str, caller: CallerIdentity | None
) -> Connector | None:
    rec = connection_store.get_connection(session, connection)
    if rec is None:
        return None
    # The credential seam. The org secret is the default; a resolver installed by
    # an operator (per-user warehouse identity via their own IdP automation) can
    # return something else keyed on `caller`. See services/lenses/credential_resolver.
    org_secret = connection_store.get_secret(session, connection)
    secret = credential_resolver.resolve(
        credential_resolver.CredentialRequest(
            caller=caller,
            connection=rec.name,
            connection_type=rec.type,
            config=rec.config,
            org_secret=org_secret,
        )
    )
    return _build_from_record(rec, secret)


def resolve_connector(
    connection: str,
    org_id: uuid.UUID | str | None = None,
    *,
    session: Session | None = None,
    caller: CallerIdentity | None = None,
) -> Connector:
    """The org's connector for *connection*, or the dev/seed built-in.

    ``session``, when given, reads the connection on the CALLER's transaction —
    the only way to see one staged by an in-flight apply (a gate that opened its
    own session raised `unknown connection` on the FIRST apply of a project,
    where dst.yaml and the lens's eval cases arrive in the same push).

    ``caller`` is the person behind a data-plane request, passed through to the
    credential seam. It is None on the management plane, where operations run as the
    org and the org service account is always the right credential."""
    found = None
    if session is not None:
        found = _from_session(session, connection, caller)
    elif org_id is not None:
        with org_session(org_id) as own:
            found = _from_session(own, connection, caller)
    return found if found is not None else _builtin(connection)
