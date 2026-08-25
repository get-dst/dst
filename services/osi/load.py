"""An OSI semantic model -> dst `semantic/entities/*.yaml` assets.

The structural half is faithful: datasets become entities, fields become fields (or
dimensions when they carry a real expression), relationships become joins. The
relationship direction is the valuable part — the spec defines `from` as the many side
and `to` as the one side, which is exactly the cardinality the compiler needs to decide
whether a join may be emitted, so an imported model arrives with safe joins declared
rather than guessed.

Metrics are the lossy half and are treated honestly. OSI carries a metric as a full SQL
aggregate (`SUM(orders.amount)`) with no separate agg/column, while dst's metric is
structured so it can be recomposed, filtered and grain-shifted. A metric whose
expression parses as ONE aggregate over ONE column of ONE dataset is reconstructed;
anything else — a window, a CASE, arithmetic over several datasets — is skipped with
its reason rather than half-imported into something that would compile to the wrong
number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import sqlglot
from sqlglot import exp

from services.contracts.semantic_model import Dimension, EntitySource, Field, FieldType, Metric
from services.contracts.shared_semantic import SharedEntity, SharedJoin

# The spec's portable datatypes -> dst's `fields[].type`.
_FIELD_TYPE: dict[str, FieldType] = {
    "string": "string",
    "integer": "integer",
    "decimal": "number",
    "float": "number",
    "boolean": "boolean",
    "date": "date",
    "time": "string",
    "datetime": "timestamp",
    "datetimetz": "timestamp",
    "opaque": "json",
}

_AGG_NODES: dict[type[exp.Expression], str] = {
    exp.Sum: "sum",
    exp.Avg: "avg",
    exp.Min: "min",
    exp.Max: "max",
    exp.Count: "count",
}


@dataclass
class OsiImport:
    entities: list[SharedEntity] = field(default_factory=list)
    ai_context: str | None = None
    use_when: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def read_ai_context(value: Any) -> tuple[str, list[str]]:
    """``(instructions, examples)`` from either shape ai_context is allowed to take.

    osi-schema.json says ``oneOf: [string, object]``, and both occur in the wild —
    the working group's own TPC-DS example uses the object with `instructions` and
    `synonyms` while spec.yaml documents a bare string. Reading only one shape
    crashes on real-world files.
    """
    if not value:
        return "", []
    if isinstance(value, str):
        return value, []
    if not isinstance(value, dict):
        return str(value), []
    parts = [str(value["instructions"])] if value.get("instructions") else []
    synonyms = value.get("synonyms") or []
    if isinstance(synonyms, list) and synonyms:
        parts.append("Also called: " + ", ".join(str(s) for s in synonyms))
    examples = value.get("examples") or []
    return "\n".join(parts), [str(e) for e in examples] if isinstance(examples, list) else []


def _expression_sql(node: Any, prefer: str) -> str | None:
    """The expression string for a field/metric, preferring the caller's dialect."""
    dialects = (node or {}).get("dialects") or []
    if not isinstance(dialects, list) or not dialects:
        return None
    for entry in dialects:
        if isinstance(entry, dict) and str(entry.get("dialect", "")).upper() == prefer.upper():
            return cast(str | None, entry.get("expression"))
    first = dialects[0]
    return cast(str | None, first.get("expression")) if isinstance(first, dict) else None


def _as_metric(name: str, sql: str, dataset: str, description: str | None) -> Metric | None:
    """Reconstruct a structured metric from one OSI aggregate expression, or None."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001
        return None
    if tree is None:
        return None
    aggs = [n for n in tree.walk() if type(n) in _AGG_NODES]
    if len(aggs) != 1 or aggs[0] is not tree:
        return None  # zero, several, or wrapped in arithmetic — not a simple metric
    agg = aggs[0]
    inner = agg.this
    distinct = isinstance(inner, exp.Distinct)
    if distinct:
        expressions = inner.expressions
        inner = expressions[0] if expressions else None
    if isinstance(inner, exp.Star) and isinstance(agg, exp.Count):
        return Metric(name=name, agg="count", description=description)
    if not isinstance(inner, exp.Column):
        return None
    if inner.table and inner.table != dataset:
        return None  # reaches into another dataset; dst metrics are entity-local
    # sqlglot 30 types aggregate nodes as Expr rather than Expression; same cast the
    # runtime guards use on aggregate nodes.
    agg_name = _AGG_NODES[cast(type[exp.Expression], type(agg))]
    if distinct:
        if agg_name != "count":
            return None
        agg_name = "count_distinct"
    return Metric(
        name=name,
        agg=cast(Any, agg_name),
        expr=f"{dataset}.{inner.name}",
        description=description,
    )


def from_osi(document: dict[str, Any], *, connection: str, dialect: str = "ANSI_SQL") -> OsiImport:
    """Translate a parsed OSI document into shared entities + an honest skip list."""
    models = document.get("semantic_model")
    if isinstance(models, dict):  # a single model, unwrapped
        models = [models]
    if not isinstance(models, list) or not models:
        raise ValueError("no `semantic_model` in the document — is this an OSI model file?")
    if len(models) > 1:
        raise ValueError(
            f"{len(models)} semantic models in one file; import them one at a time so each "
            "becomes its own dst layer"
        )
    model = models[0]

    model_instructions, model_examples = read_ai_context(model.get("ai_context"))
    out = OsiImport(ai_context=model_instructions or None, use_when=model_examples)
    datasets = model.get("datasets") or []
    by_name: dict[str, SharedEntity] = {}

    for ds in datasets:
        name = ds.get("name")
        source = ds.get("source")
        if not name or not source:
            out.skipped.append(f"dataset {name or '(unnamed)'}: needs both `name` and `source`")
            continue
        fields: list[Field] = []
        dimensions: list[Dimension] = []
        default_time: str | None = None
        for f in ds.get("fields") or []:
            fname = f.get("name")
            if not fname:
                continue
            ftype = _FIELD_TYPE.get(str(f.get("datatype", "")).lower(), "string")
            sql = _expression_sql(f.get("expression"), dialect) or fname
            bare = sql.strip() in (fname, f"{name}.{fname}")
            if bare:
                fields.append(Field(name=fname, type=ftype, description=f.get("description")))
            else:
                # A computed field is a dst dimension; it still needs a physical
                # field to exist for sql_guard, which only the warehouse can supply —
                # so record the expression and say the column list needs introspecting.
                dimensions.append(
                    Dimension(name=fname, expr=sql, type=ftype, description=f.get("description"))
                )
            dim = f.get("dimension") or {}
            if isinstance(dim, dict) and dim.get("is_time") and default_time is None:
                default_time = fname
        entity = SharedEntity(
            name=name,
            description=ds.get("description"),
            grain=None,
            source=EntitySource(connection=connection, table=source),
            primary_key=list(ds.get("primary_key") or []),
            default_time_field=default_time,
            fields=fields,
            dimensions=dimensions,
        )
        # The spec's per-dataset ai_context is where the judgment lives: instructions
        # become use_cases (they reach the generation prompt and the router), and the
        # spec's own "sample questions or use cases" become common_questions.
        instructions, examples = read_ai_context(ds.get("ai_context"))
        if instructions:
            entity.use_cases = [line for line in instructions.splitlines() if line.strip()]
        if examples:
            entity.common_questions = examples
        by_name[name] = entity
        out.entities.append(entity)

    for rel in model.get("relationships") or []:
        many, one = rel.get("from"), rel.get("to")
        from_cols = rel.get("from_columns") or []
        to_cols = rel.get("to_columns") or []
        label = rel.get("name") or f"{many} -> {one}"
        if many not in by_name or one not in by_name:
            out.skipped.append(f"relationship '{label}': names a dataset that is not in the file")
            continue
        if len(from_cols) != len(to_cols) or not from_cols:
            out.skipped.append(f"relationship '{label}': from_columns/to_columns do not pair up")
            continue
        on = " AND ".join(
            f"{many}.{a} = {one}.{b}" for a, b in zip(from_cols, to_cols, strict=True)
        )
        # `from` is the many side by the spec's own definition, which is precisely the
        # cardinality the compiler needs — an imported join is safe by construction.
        by_name[many].joins.append(SharedJoin(right=one, on=on, relationship="many_to_one"))

    for m in model.get("metrics") or []:
        mname = m.get("name")
        sql = _expression_sql(m.get("expression"), dialect)
        if not mname or not sql:
            out.skipped.append(f"metric '{mname or '(unnamed)'}': no expression")
            continue
        owner = _owning_dataset(sql, set(by_name))
        if owner is None:
            out.skipped.append(
                f"metric '{mname}': `{sql}` does not resolve to exactly one dataset — "
                "dst metrics belong to one entity"
            )
            continue
        metric = _as_metric(mname, sql, owner, m.get("description"))
        if metric is None:
            out.skipped.append(
                f"metric '{mname}': `{sql}` is not one aggregate over one column — "
                "re-author it as a dst metric, or keep it as a definition"
            )
            continue
        by_name[owner].metrics.append(metric)

    return out


def _owning_dataset(sql: str, known: set[str]) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001
        return None
    if tree is None:
        return None
    owners = {c.table for c in tree.find_all(exp.Column) if c.table in known}
    if len(owners) == 1:
        return owners.pop()
    if not owners and len(known) == 1:
        return next(iter(known))
    return None
