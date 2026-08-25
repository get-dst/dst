"""dst shared semantic layer -> an OSI semantic model.

Mapping, and where it is lossy (every lossy edge is reported, never silently dropped):

  entity                 -> dataset (name, source, primary_key, description)
  grain/use_cases/
    common_questions     -> dataset.ai_context   (the slot the spec reserves for us)
  field                  -> dataset.field with expression + datatype
  dimension              -> dataset.field carrying the authored expression
  metric                 -> model-level metric, expression = the compiled aggregate
  join                   -> relationship, ALWAYS written many -> one

OSI relationships are directional by definition: `from` is the many side, `to` is the
one side. That is the same fact `Join.relationship` carries, so a `many_to_one` join
writes `from=left`, and a `one_to_many` writes `from=right`. A join whose relationship
is undeclared cannot be written honestly and is skipped with that reason — the spec has
no way to say "unknown cardinality", and guessing is how a metric layer reports seven
times the truth.
"""

from __future__ import annotations

from typing import Any

from services.contracts.semantic_model import Definition, Entity, Metric
from services.contracts.shared_semantic import SharedEntity
from services.runtime.compiler import CompileError, metric_sql

OSI_VERSION = "0.2.0"

# dst's `fields[].type` -> the spec's portable datatype vocabulary. `json` has no
# portable equivalent, which is exactly what Opaque is for.
_DATATYPE = {
    "string": "String",
    "integer": "Integer",
    "number": "Decimal",
    "boolean": "Boolean",
    "date": "Date",
    "timestamp": "DateTime",
    "json": "Opaque",
}

# dst dialects -> the spec's dialect enum. Three of ours have no entry and are
# close enough to standard SQL that ANSI_SQL is the honest label.
_DIALECT = {
    "bigquery": "BIGQUERY",
    "snowflake": "SNOWFLAKE",
    "duckdb": "ANSI_SQL",
    "postgres": "ANSI_SQL",
    "mysql": "ANSI_SQL",
}


def _expression(sql: str, dialect: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": _DIALECT.get(dialect, "ANSI_SQL"), "expression": sql}]}


def _entity_ai_context(entity: Entity) -> dict[str, Any]:
    """The judgment dst carries that a plain schema does not.

    This is the reason to bother with the format at all: `grain` ("one row per
    transaction, no id column"), `use_cases` ("avoid for region history"), and
    `common_questions` are precisely what a consuming agent needs and what an
    FK-and-column dump cannot say.

    Written as the schema's OBJECT form (``instructions``/``synonyms``/``examples``)
    rather than a prose blob: the spec reserves ``examples`` for "sample questions or
    use cases", which is exactly what `common_questions` is, and a consumer can use
    the pieces separately. (spec.yaml documents ai_context as a string; the
    machine-readable osi-schema.json is the authority and allows both. The examples
    the working group ships use the object.)
    """
    instructions: list[str] = []
    if entity.grain:
        instructions.append(f"Grain: {entity.grain}")
    instructions.extend(entity.use_cases)
    context: dict[str, Any] = {}
    if instructions:
        context["instructions"] = "\n".join(instructions)
    if entity.common_questions:
        context["examples"] = list(entity.common_questions)
    return context


def _model_ai_context(
    ai_instructions: str | None, use_when: list[str], definitions: list[Definition]
) -> dict[str, Any]:
    """Model-level context: instructions + the governed vocabulary, + use_when as examples."""
    parts: list[str] = []
    if ai_instructions:
        parts.append(ai_instructions)
    # Definitions are prose governance with no OSI home of their own. Carrying them
    # in ai_context keeps the meaning with the model instead of losing it at the
    # border; an ambiguous term keeps its warning, because a consumer that answers
    # such a question confidently is the failure the status exists to prevent.
    for d in definitions:
        if d.status == "ambiguous":
            options = "; ".join(d.possible_mappings)
            suffix = f" ({options})" if options else ""
            parts.append(f"AMBIGUOUS TERM '{d.term}': {d.body}{suffix}")
        else:
            parts.append(f"{d.term}: {d.body}")
    context: dict[str, Any] = {}
    if parts:
        context["instructions"] = "\n\n".join(parts)
    if use_when:
        context["examples"] = list(use_when)
    return context


def _metric_entry(metric: Metric, entity: Entity, dialect: str) -> dict[str, Any] | None:
    """A model-level OSI metric, or None when the aggregate cannot be compiled."""
    try:
        sql = metric_sql(metric, entity)
    except CompileError:
        return None
    entry: dict[str, Any] = {
        "name": metric.name,
        "expression": _expression(sql, dialect),
    }
    if metric.description:
        entry["description"] = metric.description
    if metric.format:
        entry["ai_context"] = {"instructions": f"Display as {metric.format}."}
    return entry


def to_osi(
    entities: list[SharedEntity],
    *,
    name: str,
    dialect: str,
    description: str | None = None,
    ai_instructions: str | None = None,
    use_when: list[str] | None = None,
    definitions: list[Definition] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """``(osi_model, skipped)`` — the model as a plain dict, ready to dump as YAML."""
    skipped: list[str] = []
    datasets: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    known = {e.name for e in entities}

    for entity in entities:
        fields: list[dict[str, Any]] = []
        for f in entity.fields:
            entry: dict[str, Any] = {
                "name": f.name,
                "expression": _expression(f"{entity.name}.{f.name}", dialect),
                "datatype": _DATATYPE.get(f.type, "Opaque"),
            }
            if f.description:
                entry["description"] = f.description
            if f.name == entity.default_time_field:
                entry["dimension"] = {"is_time": True}
            fields.append(entry)
        for d in entity.dimensions:
            entry = {
                "name": d.name,
                "expression": _expression(d.expr or f"{entity.name}.{d.name}", dialect),
                "datatype": _DATATYPE.get(d.type, "Opaque"),
                "dimension": {},
            }
            if d.description:
                entry["description"] = d.description
            fields.append(entry)

        dataset: dict[str, Any] = {
            "name": entity.name,
            "source": entity.source.table,
            "fields": fields,
        }
        if entity.primary_key:
            dataset["primary_key"] = list(entity.primary_key)
        if entity.description:
            dataset["description"] = entity.description
        context = _entity_ai_context(entity)
        if context:
            dataset["ai_context"] = context
        datasets.append(dataset)

        for metric in entity.metrics:
            entry_or_none = _metric_entry(metric, entity, dialect)
            if entry_or_none is None:
                skipped.append(f"metric '{metric.name}': its aggregate does not compile")
            else:
                metrics.append(entry_or_none)

        for join in entity.joins:
            if join.right not in known:
                skipped.append(f"join {entity.name} -> {join.right}: no such entity")
                continue
            if join.relationship == "one_to_many":
                many, one = join.right, entity.name
            elif join.relationship in ("many_to_one", "one_to_one"):
                many, one = entity.name, join.right
            else:
                skipped.append(
                    f"join {entity.name} -> {join.right}: no declared relationship, and OSI "
                    "relationships are directional (from = the many side)"
                )
                continue
            from_cols, to_cols = _join_columns(join.on, many, one)
            if not from_cols or not to_cols:
                skipped.append(
                    f"join {entity.name} -> {join.right}: could not read one column per side "
                    f"out of `{join.on}`"
                )
                continue
            relationships.append(
                {
                    "name": f"{many}_to_{one}",
                    "from": many,
                    "to": one,
                    "from_columns": from_cols,
                    "to_columns": to_cols,
                }
            )

    model: dict[str, Any] = {"name": name, "datasets": datasets}
    if description:
        model["description"] = description
    context = _model_ai_context(ai_instructions, use_when or [], definitions or [])
    if context:
        model["ai_context"] = context
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    return {"version": OSI_VERSION, "semantic_model": [model]}, skipped


def _join_columns(on: str, many: str, one: str) -> tuple[list[str], list[str]]:
    """Columns each side matches on, in corresponding order (the spec requires pairing).

    Walks the ON clause's equalities so composite keys stay aligned — a
    ``a.x = b.x AND a.y = b.y`` join must not emit ``[x, y]`` against ``[y, x]``.
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(on, read="duckdb")
    except Exception:  # noqa: BLE001 — an unparseable ON is a report, not a crash
        return [], []
    from_cols: list[str] = []
    to_cols: list[str] = []
    for eq in tree.find_all(exp.EQ):
        left, right = eq.this, eq.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        by_table = {left.table: left.name, right.table: right.name}
        if many in by_table and one in by_table:
            from_cols.append(by_table[many])
            to_cols.append(by_table[one])
    return from_cols, to_cols
