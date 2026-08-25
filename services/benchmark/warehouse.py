"""The warehouse seam: lanes execute and introspect through this, never a driver.

Without this seam the harness hardwires one engine at every point that runs or
introspects SQL — the agentic SQL tool, both lane classes, the world helpers,
and the agent protocol prompt that names the dialect in prose — and a
non-default warehouse can only be reached by overwriting private lane
attributes from outside. So: one protocol, two implementations, selected by
``--warehouse``.

``DuckDBWarehouse`` delegates to the ``world.py`` helpers unchanged — the
default path emits byte-identical prompts and reports. ``SnowflakeWarehouse``
introspects the declared schemas through the real ``SnowflakeConnector``, so a
Snowflake leg exercises the same connector production uses. Loading the world
INTO Snowflake stays a separate, deliberate step: the harness never writes to a
customer-shaped warehouse.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    FieldType,
    SemanticModel,
    warehouse_field_type,
)

from . import world

if TYPE_CHECKING:
    from services.connectors.snowflake import SnowflakeConnector

    from .lanes import LaneAnswer


class Warehouse(Protocol):
    """What a lane needs from a warehouse — nothing more."""

    dialect: str  # SemanticModel dialect: "duckdb" | "snowflake"
    dialect_word: str  # the prose word the agent protocol prompt uses

    def execute(self, sql: str) -> LaneAnswer: ...

    def schema_summary(self) -> str: ...

    def profile_context(self) -> str: ...

    def structural_model(self, *, include: set[str] | None = None) -> SemanticModel: ...

    def tables(self) -> list[str]: ...

    def connector(self) -> Any: ...  # the runtime pipeline's execute seam


def coerce(source: Path | str | Warehouse) -> Warehouse:
    """A lane built with a plain path gets the DuckDB warehouse — every
    pre-seam call site keeps working unchanged."""
    if isinstance(source, (Path, str)):
        return DuckDBWarehouse(Path(source))
    return source


class DuckDBWarehouse:
    """The default: the local artifact, through the pre-seam helpers verbatim."""

    dialect = "duckdb"
    dialect_word = "DuckDB"

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def execute(self, sql: str) -> LaneAnswer:
        import duckdb

        from .lanes import LaneAnswer

        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = [list(r) for r in cur.fetchmany(200)]
            return LaneAnswer(columns=cols, rows=rows, sql=sql)
        except Exception as exc:  # noqa: BLE001 — observation, not crash
            return LaneAnswer(columns=[], rows=[], sql=sql, error=str(exc))
        finally:
            con.close()

    def schema_summary(self) -> str:
        return world.schema_summary(self.db_path)

    def profile_context(self) -> str:
        return world.profile_context(self.db_path)

    def structural_model(self, *, include: set[str] | None = None) -> SemanticModel:
        return world.structural_model(self.db_path, include=include)

    def tables(self) -> list[str]:
        return world.list_tables(self.db_path)

    def connector(self) -> Any:
        from services.connectors.duckdb import DuckDBConnector

        return DuckDBConnector(str(self.db_path), profile=False)


class SnowflakeWarehouse:
    """The live-trial leg: introspection and execution through the production
    ``SnowflakeConnector``, scoped to the schemas the world's dataset dirs name.

    Rows come back capped at 200 like the DuckDB tool (``row_limit`` wraps the
    query, which truncates identically to ``fetchmany(200)``); NUMBER columns
    arrive as ``Decimal``, which the grader already coerces.
    """

    dialect = "snowflake"
    dialect_word = "Snowflake"

    def __init__(self, conn: SnowflakeConnector, schemas: tuple[str, ...]) -> None:
        self._conn = conn
        self._schemas = tuple(s.upper() for s in schemas)

    @classmethod
    def from_env(cls, env: dict[str, str], schemas: tuple[str, ...]) -> SnowflakeWarehouse:
        """Credentials from SNOWFLAKE_* env vars; keypair when a key path is
        set (passwords are on Snowflake's retirement clock), PAT otherwise."""
        from services.connectors.snowflake import SnowflakeConnector

        key_path = env.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
        pem = Path(key_path).read_text() if key_path and Path(key_path).exists() else None
        return cls(
            SnowflakeConnector(
                account=env["SNOWFLAKE_ACCOUNT"],
                user=env["SNOWFLAKE_USER"],
                password=None if pem else env.get("SNOWFLAKE_PAT"),
                private_key=pem,
                warehouse=env["SNOWFLAKE_WAREHOUSE"],
                database=env["SNOWFLAKE_DATABASE"],
                schema=None,
                role=env.get("SNOWFLAKE_ROLE"),
                statement_timeout_ms=120000,
            ),
            schemas,
        )

    def execute(self, sql: str) -> LaneAnswer:
        from .lanes import LaneAnswer

        try:
            r = self._conn.execute(sql, row_limit=200)
            return LaneAnswer(columns=list(r.columns), rows=[list(x) for x in r.rows], sql=sql)
        except Exception as exc:  # noqa: BLE001 — observation, not crash
            return LaneAnswer(columns=[], rows=[], sql=sql, error=str(exc))

    @cached_property
    def _catalog(self) -> list[tuple[str, str, str, str]]:
        r = self._conn.execute(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            f"WHERE table_schema IN {self._schemas!r} "
            "ORDER BY table_schema, table_name, ordinal_position"
        )
        return [(str(s), str(t), str(c), str(d)) for s, t, c, d in r.rows]

    def schema_summary(self) -> str:
        lines: list[str] = []
        current = None
        for schema, table, column, dtype in self._catalog:
            key = f"{schema}.{table}"
            if key != current:
                lines.append(f"\n{key}:")
                current = key
            lines.append(f"  {column} {dtype}")
        return "\n".join(lines).strip()

    def profile_context(self, *, max_categorical: int = 12) -> str:
        lines = ["# Data dictionary (profiled from the warehouse)"]
        for fq in self.tables():
            count = int(str(self._conn.execute(f"SELECT COUNT(*) FROM {fq}").rows[0][0]))
            lines.append(f"\n## {fq} ({count} rows)")
            if count <= 20:
                # Reference/decode tables: full content beats per-column values —
                # the code↔label *pairs* are the knowledge (the win-rate lesson).
                full = self._conn.execute(f"SELECT * FROM {fq}")
                lines.append("  " + " | ".join(full.columns))
                for row in full.rows:
                    lines.append("  " + " | ".join(str(c) for c in row))
                continue
            schema, table = fq.split(".", 1)
            for s, t, column, dtype in self._catalog:
                if (s, t) != (schema, table) or dtype not in ("TEXT", "VARCHAR"):
                    continue
                distinct = self._conn.execute(
                    f"SELECT DISTINCT {column} FROM {fq} WHERE {column} IS NOT NULL "
                    f"LIMIT {max_categorical + 1}"
                ).rows
                if 0 < len(distinct) <= max_categorical:
                    values = ", ".join(sorted(str(v) for (v,) in distinct))
                    lines.append(f"- {column}: one of [{values}]")
        return "\n".join(lines)

    def structural_model(self, *, include: set[str] | None = None) -> SemanticModel:
        fields_by_table: dict[str, list[Field]] = {}
        for schema, table, column, dtype in self._catalog:
            fq = f"{schema}.{table}"
            if include is not None and fq not in include:
                continue
            fields_by_table.setdefault(fq, []).append(
                Field(name=column, type=cast(FieldType, warehouse_field_type(dtype) or "string"))
            )
        entities = [
            Entity(
                name=table.replace(".", "_"),
                source=EntitySource(connection="snowflake:benchmark", table=table),
                fields=fields,
            )
            for table, fields in sorted(fields_by_table.items())
        ]
        return SemanticModel(lens="proving_ground", dialect="snowflake", entities=entities)

    def tables(self) -> list[str]:
        seen: dict[str, None] = {}
        for schema, table, _, _ in self._catalog:
            seen.setdefault(f"{schema}.{table}")
        return list(seen)

    def connector(self) -> Any:
        return self._conn
