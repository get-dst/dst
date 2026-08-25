"""Build the DuckDB proving-ground artifact from the generator's CSV output.

The company generator (``generate_company.py``) emits
``<root>/<dataset>/<table>.csv`` plus ``_oracle.json``. This module loads the
CSVs into a DuckDB file with one schema per dataset, so SQL addresses tables as
``crm.customers``, ``finance.invoices`` — the same shape the BigQuery twin uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import duckdb

from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    FieldType,
    SemanticModel,
    warehouse_field_type,
)


def load_world(csv_root: Path, db_path: Path) -> list[str]:
    """Load every ``<dataset>/<table>.csv`` under ``csv_root`` into ``db_path``.

    Returns the fully-qualified table names loaded. Overwrites an existing file
    so a benchmark run is always against a freshly-built artifact.
    """
    csv_root = Path(csv_root)
    if db_path.exists():
        db_path.unlink()
    loaded: list[str] = []
    con = duckdb.connect(str(db_path))
    try:
        for dataset_dir in sorted(p for p in csv_root.iterdir() if p.is_dir()):
            schema = dataset_dir.name
            con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            for csv_file in sorted(dataset_dir.glob("*.csv")):
                table = csv_file.stem
                con.execute(
                    f'CREATE TABLE "{schema}"."{table}" AS '
                    f"SELECT * FROM read_csv_auto(?, header=true)",
                    [str(csv_file)],
                )
                loaded.append(f"{schema}.{table}")
    finally:
        con.close()
    if not loaded:
        raise FileNotFoundError(f"no <dataset>/<table>.csv files under {csv_root}")
    return loaded


def load_oracle(path: Path) -> dict[str, object]:
    oracle: dict[str, object] = json.loads(Path(path).read_text(encoding="utf-8"))
    return oracle


def schema_summary(db_path: Path) -> str:
    """A plain schema listing — exactly what the *baseline* lane gets: tables,
    columns, types. No descriptions, no business context. That is the point."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('information_schema','main','pg_catalog') "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
    finally:
        con.close()
    lines: list[str] = []
    current = None
    for schema, table, column, dtype in rows:
        key = f"{schema}.{table}"
        if key != current:
            lines.append(f"\n{key}:")
            current = key
        lines.append(f"  {column} {dtype}")
    return "\n".join(lines).strip()


def profile_context(db_path: Path, *, max_categorical: int = 12) -> str:
    """A data dictionary synthesized from the live world: row counts plus the
    actual values of low-cardinality text columns. This is the profiler's
    catalog+sampling pass in miniature — profile-derived context, mechanically
    produced, never authored against a known miss."""
    con = duckdb.connect(str(db_path), read_only=True)
    lines: list[str] = ["# Data dictionary (profiled from the warehouse)"]
    try:
        tables = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema','main','pg_catalog') "
            "ORDER BY 1, 2"
        ).fetchall()
        for schema, table in tables:
            fq = f'"{schema}"."{table}"'
            (count,) = con.execute(f"SELECT count(*) FROM {fq}").fetchone()  # type: ignore[misc]
            lines.append(f"\n## {schema}.{table} ({count} rows)")
            if count <= 20:
                # Reference/decode tables: full content beats per-column values —
                # the code↔label *pairs* are the knowledge.
                cur = con.execute(f"SELECT * FROM {fq}")
                header = [d[0] for d in cur.description or []]
                lines.append("  " + " | ".join(header))
                for row in cur.fetchall():
                    lines.append("  " + " | ".join(str(c) for c in row))
                continue
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [schema, table],
            ).fetchall()
            for column, dtype in cols:
                if dtype == "VARCHAR":
                    distinct = con.execute(
                        f'SELECT DISTINCT "{column}" FROM {fq} '
                        f'WHERE "{column}" IS NOT NULL LIMIT {max_categorical + 1}'
                    ).fetchall()
                    if 0 < len(distinct) <= max_categorical:
                        values = ", ".join(sorted(str(v) for (v,) in distinct))
                        lines.append(f"- {column}: one of [{values}]")
    finally:
        con.close()
    return "\n".join(lines)


def _field_type(duck_type: str) -> FieldType:
    return cast(FieldType, warehouse_field_type(duck_type) or "string")


def scope_from_certified_defs(definitions_text: str, all_tables: list[str]) -> set[str]:
    """Curation-derived table scope: all silver + gold, plus only the bronze
    tables the certified names. The lens-author move — derived mechanically from
    the definitions, never from any question or miss."""
    scope = {t for t in all_tables if t.startswith(("silver.", "gold."))}
    for t in all_tables:
        if t.startswith("bronze.") and t.split(".", 1)[1] in definitions_text:
            scope.add(t)
    return scope


def list_tables(db_path: Path) -> list[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema','main','pg_catalog') ORDER BY 1, 2"
        ).fetchall()
    finally:
        con.close()
    return [f"{s}.{t}" for s, t in rows]


def structural_model(
    db_path: Path, *, lens: str = "proving_ground", include: set[str] | None = None
) -> SemanticModel:
    """A purely structural SemanticModel — every table, every column, no curated
    definitions. The dst lane starts here; context, definitions, and
    instructions are added (or stripped) on top by the lane config.

    Built from ``information_schema`` directly because the world is
    schema-qualified (``crm.customers``) and ``DuckDBConnector.introspect()``
    today only sees the current schema.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('information_schema','main','pg_catalog') "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
    finally:
        con.close()
    fields_by_table: dict[str, list[Field]] = {}
    for schema, table, column, dtype in rows:
        fq = f"{schema}.{table}"
        if include is not None and fq not in include:
            continue
        fields_by_table.setdefault(fq, []).append(Field(name=column, type=_field_type(dtype)))
    entities = [
        Entity(
            name=table.replace(".", "_"),
            source=EntitySource(connection=str(db_path), table=table),
            fields=fields,
        )
        for table, fields in sorted(fields_by_table.items())
    ]
    return SemanticModel(lens=lens, dialect="duckdb", entities=entities)
