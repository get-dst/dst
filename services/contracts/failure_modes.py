"""The failure-mode vocabulary: twenty names for why an answer did not come
back clean.

A mode is a governed outcome the caller can read: every clarification,
refusal, capped answer, failed eval case, observe row, receipt and plan line
carries exactly one. A fault (dst emitted invalid SQL, the warehouse timed
out) is not a mode; it stays ``status: error`` and fails loud.

Each mode names the earliest point it is detected and the file that fixes it,
so a scorecard by mode reads as a list of work: ``unbound_value`` is a
dictionary, ``period_unreadable`` is the window parser, ``hidden_column`` is
security. The older vocabulary (``ClarificationRequest.kind``, the guard's
``policy``/``syntax``, the decider outage markers, drift kinds) is mapped onto
these names here, in one place, so the two never drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class FailureMode(StrEnum):
    # routing
    unroutable = "unroutable"
    misrouted = "misrouted"
    # resolve
    uncovered_word = "uncovered_word"
    unasked_filter = "unasked_filter"
    unbound_value = "unbound_value"
    ambiguous_metric = "ambiguous_metric"
    not_declared = "not_declared"
    not_expressible = "not_expressible"
    period_unreadable = "period_unreadable"
    window_misbound = "window_misbound"
    # verify
    data_gap = "data_gap"
    empty_result = "empty_result"
    wrong_grain = "wrong_grain"
    partial_ask = "partial_ask"
    scope_violation = "scope_violation"
    hidden_column = "hidden_column"
    external_access = "external_access"
    unverifiable = "unverifiable"
    stale = "stale"
    provider_down = "provider_down"


# Where each mode is raised: the earliest deterministic point, and the one
# place per request it is reported from.
STAGE: dict[FailureMode, str] = {
    FailureMode.unroutable: "routing",
    FailureMode.misrouted: "routing",
    FailureMode.uncovered_word: "verify",
    FailureMode.unasked_filter: "resolve",
    FailureMode.unbound_value: "resolve",
    FailureMode.ambiguous_metric: "resolve",
    FailureMode.not_declared: "resolve",
    FailureMode.not_expressible: "resolve",
    FailureMode.period_unreadable: "resolve",
    FailureMode.window_misbound: "verify",
    FailureMode.data_gap: "verify",
    FailureMode.empty_result: "verify",
    FailureMode.wrong_grain: "verify",
    FailureMode.partial_ask: "verify",
    FailureMode.scope_violation: "verify",
    FailureMode.hidden_column: "verify",
    FailureMode.external_access: "verify",
    FailureMode.unverifiable: "verify",
    FailureMode.stale: "serve",
    FailureMode.provider_down: "serve",
}

# The slot a typed clarification could not fill (``ClarificationRequest.term``
# on ``kind="unresolved_slot"``) → the mode. Any other term is a column, a
# value or a word the question used, and the construction site names the mode
# itself; the fallback is the value slot, the commonest.
_SLOT_MODES: dict[str, FailureMode] = {
    "window": FailureMode.period_unreadable,
    "compare": FailureMode.period_unreadable,
    "days": FailureMode.period_unreadable,
    "range": FailureMode.period_unreadable,
    "dimension": FailureMode.wrong_grain,
    "grain": FailureMode.wrong_grain,
    "order": FailureMode.wrong_grain,
    "entity": FailureMode.not_declared,
    "shape": FailureMode.uncovered_word,
    "time_field": FailureMode.ambiguous_metric,
    "filter": FailureMode.unbound_value,
    "filter_value": FailureMode.unbound_value,
}


def clarification_mode(kind: str, term: str, options: list[str]) -> FailureMode:
    """The mode a clarification carries when its construction site did not
    name one: the older ``kind`` vocabulary, split by the slot where one kind
    covered several modes. A metric slot with candidates is a choice between
    declared metrics; with none, a word the layer does not cover."""
    if kind == "ambiguous_term":
        return FailureMode.ambiguous_metric
    if kind == "unknown_value":
        return FailureMode.unbound_value
    if kind == "named_table":
        return FailureMode.scope_violation
    if term == "metric":
        return FailureMode.ambiguous_metric if options else FailureMode.uncovered_word
    return _SLOT_MODES.get(term, FailureMode.unbound_value)


# Plan and validation codes → the mode each one prevents at serve time, so a
# scorecard's "file that fixes it" is a lookup. A code absent here is a
# structural or hygiene finding with no serve-time mode.
HINT_MODES: dict[str, FailureMode] = {
    "router_anchor": FailureMode.unroutable,
    "ambiguous_audience_unmapped": FailureMode.unroutable,
    "competing_metric_claim": FailureMode.ambiguous_metric,
    "twin_metric_filters": FailureMode.ambiguous_metric,
    "status_done_ambiguous": FailureMode.ambiguous_metric,
    "ambiguous_term_unreachable": FailureMode.ambiguous_metric,
    "dimension_alias_conflict": FailureMode.ambiguous_metric,
    "duplicate_metric": FailureMode.ambiguous_metric,
    "duplicate_definition": FailureMode.ambiguous_metric,
    "value_aliases_unbound": FailureMode.unbound_value,
    "entity_no_fields": FailureMode.not_declared,
    "no_entities": FailureMode.not_declared,
    "definition_about_dangling": FailureMode.not_declared,
    "definition_cites_dangling": FailureMode.not_declared,
    "unresolved_reference": FailureMode.not_declared,
    "intent_tier_escalation_only": FailureMode.not_expressible,
    "metric_expr_cross_entity": FailureMode.not_expressible,
    "invalid_timezone": FailureMode.window_misbound,
    "clock_type": FailureMode.window_misbound,
    "additivity": FailureMode.wrong_grain,
    "population_filter_unbound": FailureMode.unasked_filter,
    "probe_out_of_scope": FailureMode.scope_violation,
    "sensitive_columns": FailureMode.hidden_column,
    "invalid_stale_after": FailureMode.stale,
    "lens_model_unservable": FailureMode.provider_down,
    "no_callers": FailureMode.provider_down,
}

# Drift kinds → the mode a reader of the drifted table serves under. The quiet
# kinds (a comment, a gained column, a new table, a shifted distribution) are
# informational and carry no mode; they never make `dst drift` exit 2.
DRIFT_MODES: dict[str, FailureMode] = {
    "stale_table": FailureMode.stale,
    "column_dropped": FailureMode.stale,
    "column_retyped": FailureMode.stale,
    "table_dropped": FailureMode.stale,
    "dictionary_changed": FailureMode.stale,
    "enum_value_added": FailureMode.stale,
    "partition_changed": FailureMode.stale,
}
