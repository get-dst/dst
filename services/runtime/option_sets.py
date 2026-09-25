"""Option sets — the closed sets a typed decision resolves a question over.

The durable half of the decision regime: whoever makes the decision (a voting
chat model today, a typed-decision model tomorrow), the OPTIONS come from what
dst already owns — the semantic model's metrics, definitions and dimensions,
the grain vocabulary, and the complete value dictionaries the column profiler
measured. Keys are the ledger's slot kinds, so a probability attaches to the
slot it was measured for. Pure: model + profiles in, options out.

Filter values come ONLY from complete dictionaries (``values_complete``): an
incomplete top-N is a sample, and a decision over a sample would refuse the
values it never saw. Only enum-sized dictionaries (LOW_CARDINALITY_MAX) become
option sets, which fits a typed-decision model's option budget by construction;
a longer complete dictionary is matched against the question verbatim instead
(services/runtime/typed_resolver.py), never offered as options.
"""

from __future__ import annotations

from typing import get_args

from services.contracts.profile import LOW_CARDINALITY_MAX, TableProfile
from services.contracts.protocols import Option
from services.contracts.query_intent import TimeGrain
from services.contracts.semantic_model import Entity, Metric, SemanticModel
from services.runtime import value_guard
from services.runtime.compiler import CompileError, metric_sql


def _metric_head(metric: Metric, entity: Entity) -> str:
    """The metric's compiled aggregate, the way the prompt shows it."""
    try:
        return metric_sql(metric, entity, inline_filters=True)
    except CompileError:
        return ""


def option_sets(
    model: SemanticModel,
    profiles: list[TableProfile],
    *,
    domains: dict[str, list[str]] | None = None,
) -> dict[str, list[Option]]:
    """``{slot: [Option]}`` for every closed decision the model + profiles support.

    ``metric`` and ``dimension`` names are qualified ``entity.name`` when the same
    bare name appears on more than one entity — the compiler's own rule.
    ``filter_value:<column>`` appears only for columns with a complete,
    enum-sized value dictionary. ``definition`` lists enforceable definitions (those with an
    ``sql_expr``); ``grain`` is the compiler's TimeGrain vocabulary.
    """
    metric_owners: dict[str, list[str]] = {}
    dim_owners: dict[str, list[str]] = {}
    for e in model.entities:
        for m in e.metrics:
            metric_owners.setdefault(m.name, []).append(e.name)
        for d in e.dimensions:
            dim_owners.setdefault(d.name, []).append(e.name)

    def _qualified(name: str, entity: str, owners: dict[str, list[str]]) -> str:
        return f"{entity}.{name}" if len(owners.get(name, [])) > 1 else name

    metrics = [
        Option(
            _qualified(m.name, e.name, metric_owners),
            " — ".join(p for p in (_metric_head(m, e), m.description or "") if p),
        )
        for e in model.entities
        for m in e.metrics
    ]
    definitions = [
        Option(d.term, d.body or "") for d in model.definitions if d.sql_expr and d.sql_expr.strip()
    ]
    dimensions = [
        Option(_qualified(d.name, e.name, dim_owners), d.description or "")
        for e in model.entities
        for d in e.dimensions
    ]
    out: dict[str, list[Option]] = {
        "metric": metrics,
        "definition": definitions,
        "dimension": dimensions,
        "grain": [Option(g) for g in get_args(TimeGrain)],
    }
    resolved = domains if domains is not None else value_guard.value_domains(model, profiles)
    for column, values in sorted(resolved.items()):
        if len(values) <= LOW_CARDINALITY_MAX:
            out[f"filter_value:{column}"] = [Option(v) for v in values]
    return out
