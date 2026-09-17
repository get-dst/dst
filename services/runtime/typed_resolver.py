"""The typed resolver — a question either types or it doesn't.

Every slot of an answer is a closed-set decision over what dst already owns
(services/runtime/option_sets.py), made by the install's ``Decider`` with a
measured probability and turned into act / clarify / decline by the policy
(services/runtime/decision_policy.py). Nothing is extracted from prose:

- shape, entity, metrics, dimensions, grain, filter columns and dictionary
  values are DECISIONS (each a DecisionRecord on the answer's ledger);
- the time window is a deterministic parse (services/runtime/timewindow.py);
  a stated window the parser cannot resolve clarifies, never guesses;
- numbers and dates in the question resolve only when unambiguous (exactly one
  standalone literal); anything else — and every open string value — comes
  from the caller's ``bindings`` or clarifies with ``unresolved_slot``, naming
  the column, its type grammar and the known values;
- order and limit are a small regex vocabulary.

The output is a ``QueryIntent`` the deterministic compiler turns into SQL — the
same closed sets the compiler already enforces, decided up front with a
probability instead of emitted as one JSON blob and rejected after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast

from services.contracts.protocols import (
    ContextChunk,
    Decider,
    Decision,
    GeneratedQuery,
    Option,
)
from services.contracts.query_intent import (
    FilterOp,
    IntentFilter,
    IntentOrder,
    QueryIntent,
    TimeGrain,
)
from services.contracts.resolution import DecisionRecord
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import Definition, Entity, SemanticModel
from services.runtime import ambiguity, decision_policy, reading, timewindow
from services.runtime.compiler import CompileError, compile_intent
from services.runtime.intent_generator import intent_term
from services.runtime.option_sets import option_sets

MAX_METRICS = 3
MAX_DIMENSIONS = 3
MAX_FILTERS = 3

_NONE = "none"
_ANOTHER_NONE = Option(_NONE, "no further one is asked for")
_SHAPES = [
    Option("aggregate", "a figure: a total, count, average, rate, or a breakdown of one"),
    Option("listing", "rows: list / show / which records, with their fields"),
]
_ENGAGED = [Option("engaged", "the term's filter applies"), Option("not_engaged", "it does not")]
# Two different "none"s on a filter value: the question states NO value for
# the field (the column was not a filter after all — dropped, acted on) versus
# a value that is not in the dictionary (an unknown value — clarified). Under
# "act unless none wins" the first must never become a clarification.
_UNSTATED = "unstated"
_STATED = [
    Option("stated", "the question states a value for this field"),
    Option(_UNSTATED, "the question states no value for it — it is not a filter"),
]
_OPS = [
    Option("=", "equal to"),
    Option(">", "greater than / more than / above"),
    Option("<", "less than / below / under"),
    Option(">=", "at least / from"),
    Option("<=", "at most / up to"),
]
_TOP = re.compile(r"\b(?:top|first|largest|biggest|highest)\s+(\d{1,3})\b", re.I)
_BOTTOM = re.compile(r"\b(?:bottom|lowest|smallest)\s+(\d{1,3})\b", re.I)
_HIGHEST = re.compile(r"\b(?:highest|largest|biggest|most|top)\b", re.I)
_LOWEST = re.compile(r"\b(?:lowest|smallest|least|bottom)\b", re.I)
_NUMBER = re.compile(r"(?<![\d.-])-?\d+(?:\.\d+)?(?![\d.])")
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_YEARISH = re.compile(r"\b(?:19|20)\d{2}\b")

GRAMMAR = {
    "date_range": "YYYY | YYYY-Qn | YYYY-MM | YYYY-MM-DD/YYYY-MM-DD",
    "date": "YYYY-MM-DD",
    "number": "a plain number",
    "string": "the exact stored value",
}


@dataclass
class TypedResolution:
    intent: QueryIntent | None
    decisions: list[DecisionRecord] = field(default_factory=list)
    clarification: ClarificationRequest | None = None
    decline: str | None = None
    # Filter columns whose value came from the caller's bindings.
    supplied: list[str] = field(default_factory=list)

    @property
    def typed(self) -> bool:
        return self.intent is not None


class _Clarify(Exception):
    def __init__(self, request: ClarificationRequest) -> None:
        self.request = request


class _Decline(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


_MONTH_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# A breakdown over time is ASKED in words — by month, weekly, daily trend,
# over time. A stated period ("in August 2026") is a window, never a grain,
# and a date-shaped literal (a batch id) is not a daily series. Only a
# question carrying one of these poses the grain question at all.
_BREAKDOWN_OVER_TIME = re.compile(
    r"\b(?:by|per|each|every)\s+(?:hour|day|week|month|quarter|year)\b"
    r"|\b(?:hourly|daily|weekly|monthly|quarterly|yearly|annually)\b"
    r"|\bover time\b|\btrend\b|\btime series\b"
    r"|\b(?:day|week|month|quarter|year)[- ](?:over|on|by)[- ](?:day|week|month|quarter|year)\b",
    re.IGNORECASE,
)


def asks_breakdown_over_time(question: str) -> bool:
    return _BREAKDOWN_OVER_TIME.search(question) is not None


def period_field(entity: Entity, domains: dict[str, list[str]]) -> str | None:
    """A string field that IS a month period ("YYYY-MM"), read off the column
    profile: every profiled value matches the form. The deterministic window
    then compiles to a range on it (`month >= '2026-01' AND month <= '2026-08'`)
    instead of asking the agent to bind the stored value — the profile turns
    an open value into a typed slot. Declared time fields win."""
    for f in entity.fields:
        if f.type != "string":
            continue
        values = domains.get(f.name.lower())
        if values and all(_MONTH_PERIOD.match(v) for v in values):
            return f.name
    return None


def _field_type(entity: Entity, name: str) -> str | None:
    for f in entity.fields:
        if f.name == name:
            return f.type
    for d in entity.dimensions:
        if d.name == name:
            return d.type
    return None


def _grammar_for(ftype: str | None) -> str:
    if ftype in ("date", "timestamp"):
        return GRAMMAR["date"]
    if ftype in ("number", "integer"):
        return GRAMMAR["number"]
    return GRAMMAR["string"]


class TypedResolver:
    def __init__(
        self,
        decider: Decider,
        *,
        domains: dict[str, list[str]] | None = None,
        entity_domains: dict[str, dict[str, list[str]]] | None = None,
        period_fields: dict[str, str] | None = None,
        bindings: dict[str, str] | None = None,
        today: date | None = None,
        policies: dict[str, decision_policy.Policy] | None = None,
    ) -> None:
        self._decider = decider
        self._domains = domains or {}
        self._entity_domains = entity_domains or {}
        self._period_fields = period_fields or {}
        self._bindings = {k.rsplit(".", 1)[-1].lower(): str(v) for k, v in (bindings or {}).items()}
        self._today = today or date.today()
        self._policies = policies

    def _period_field(self, entity: Entity) -> str | None:
        """The entity's month-period field: from the profile's sample when
        assembly read one (any sample proves the form), else from a complete
        dictionary the resolver holds."""
        declared = self._period_fields.get(entity.name)
        return declared or period_field(entity, self._domains_for(entity))

    def _domains_for(self, entity: Entity) -> dict[str, list[str]]:
        """The dictionaries this entity's decisions read: its own profile's
        first, the model-wide ones beneath (value_guard.value_domains drops a
        column name whose dictionaries differ across entities; the entity's
        own view never has to)."""
        return {**self._domains, **self._entity_domains.get(entity.name, {})}

    # ── one decision, recorded ───────────────────────────────────────────────

    def _record(
        self, slot: str, d: Decision, records: list[DecisionRecord]
    ) -> tuple[Decision, str, int]:
        verdict = decision_policy.verdict(d, slot, self._policies)
        records.append(
            DecisionRecord(
                slot=slot,
                chosen=d.chosen,
                p=d.p,
                runner_up=d.runner_up,
                margin=d.margin,
                verdict=verdict,
                provider=d.provider,
                usage=d.usage.as_dict() if d.usage is not None else None,
            )
        )
        return d, verdict, len(records) - 1

    def _decide(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        allow_none: bool,
        records: list[DecisionRecord],
        key: str | None = None,
    ) -> tuple[Decision, str, int]:
        """One decision — served from the round's fan-out when it was asked
        there (``key``), else asked now."""
        if key is not None and key in self._prefetched:
            return self._prefetched.pop(key)
        d = self._decider.decide(
            question=question, context=context, options=options, allow_none=allow_none
        )
        return self._record(slot, d, records)

    def _fan_out(
        self,
        question: str,
        specs: dict[str, tuple[str, str, list[Option], bool]],
        records: list[DecisionRecord],
    ) -> None:
        """Ask every independent question of a round in ONE call when the
        decider batches (``decide_many``); a decider without it answers each
        lazily, in the same order, when the slot is reached. Records land in
        spec order either way."""
        decide_many = getattr(self._decider, "decide_many", None)
        specs = {k: v for k, v in specs.items() if v[2]}  # nothing to ask over no options
        if decide_many is None or not specs:
            return
        answers = decide_many(
            question,
            {k: (ctx, opts, allow_none) for k, (_slot, ctx, opts, allow_none) in specs.items()},
        )
        for key, (slot, _ctx, _opts, _allow_none) in specs.items():
            self._prefetched[key] = self._record(slot, answers[key], records)

    def _peek(self, key: str) -> str | None:
        """The acted choice of a prefetched decision, or None — read, not
        consumed, so the next round can be built on it before the slot logic
        runs; the pick itself still comes from the prefetch, recorded once."""
        hit = self._prefetched.get(key)
        if hit is None:
            return None
        d, verdict, _idx = hit
        return d.chosen if verdict == "act" else None

    @staticmethod
    def _another(context: str, chosen: list[str]) -> str:
        return f"{context} Already chosen: {', '.join(chosen)}. Is another one asked for?"

    @staticmethod
    def _candidates(d: Decision, options: list[Option]) -> list[str]:
        ranked = sorted((d.probs or {}).items(), key=lambda kv: -kv[1])
        named = [k for k, _v in ranked if k != _NONE][:4]
        return named or [o.name for o in options[:4]]

    def _pick(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        allow_none: bool,
        records: list[DecisionRecord],
        clarify_term: str,
        clarify_question: str,
        decline_reason: str | None = None,
        key: str | None = None,
    ) -> str | None:
        """One single-choice slot under the policy: the chosen name on act,
        None on an acted `none`, a clarification or decline otherwise."""
        if not options:
            return None
        d, verdict, idx = self._decide(
            slot, question, context, options, allow_none=allow_none, records=records, key=key
        )
        if verdict == "act":
            return d.chosen
        if verdict == "decline":
            # A confident `none`: where the slot is the whole question (shape,
            # entity) that is a decline; elsewhere it is the honest "no more" —
            # acted on, and recorded as acted on.
            if decline_reason is not None:
                raise _Decline(decline_reason)
            records[idx] = records[idx].model_copy(update={"verdict": "act"})
            return None
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=clarify_term,
                question=clarify_question,
                options=self._candidates(d, options),
            )
        )

    def _pick_many(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        cap: int,
        records: list[DecisionRecord],
        clarify_term: str,
        first_required: bool,
        key_prefix: str | None = None,
        initial: list[str] | None = None,
    ) -> list[str]:
        """A multi-valued slot as repeated single choices: the first pick, then
        'another, or none' until none or the cap — every step recorded. Each
        pick is keyed ``<prefix>:<index>`` so a fan-out round may have asked
        it already (round two asks the first, round three the second).
        ``initial``: picks already made (the first metric, decided over every
        metric of the lens); the loop continues from there."""
        chosen: list[str] = list(initial or [])
        remaining = [o for o in options if o.name not in chosen]
        while remaining and len(chosen) < cap:
            first = not chosen
            opts = remaining if (first and first_required) else [*remaining, _ANOTHER_NONE]
            ctx = context if first else self._another(context, chosen)
            pick = self._pick(
                slot,
                question,
                ctx,
                opts,
                allow_none=not (first and first_required),
                records=records,
                clarify_term=clarify_term,
                clarify_question=(
                    f"Which {clarify_term} does the question mean? "
                    f"Candidates: {', '.join(o.name for o in remaining[:6])}."
                ),
                key=f"{key_prefix}:{len(chosen)}" if key_prefix else None,
            )
            if pick is None:
                break
            chosen.append(pick)
            remaining = [o for o in remaining if o.name != pick]
        return chosen

    # ── the slots ────────────────────────────────────────────────────────────

    def resolve(
        self, question: str, model: SemanticModel, notes: list[str] | None = None
    ) -> TypedResolution:
        """``notes``: governed facts settled before resolution (a pinned
        reading of an ambiguous term) — they ride the entity and metric
        decisions' instructions, the way they ride the JSON generator's prompt."""
        records: list[DecisionRecord] = []
        self._prefetched: dict[str, tuple[Decision, str, int]] = {}
        self._notes = " ".join(notes or [])
        try:
            intent, supplied = self._resolve(question, model, records)
        except _Clarify as c:
            return TypedResolution(
                intent=None, decisions=self._consumed(records), clarification=c.request
            )
        except _Decline as d:
            return TypedResolution(intent=None, decisions=self._consumed(records), decline=d.reason)
        return TypedResolution(intent=intent, decisions=self._consumed(records), supplied=supplied)

    def _consumed(self, records: list[DecisionRecord]) -> list[DecisionRecord]:
        """The ledger carries the decisions the resolution USED. A fan-out asks
        ahead; when an earlier slot clarified or declined, the answers still
        waiting were never part of this resolution and their raw policy verdicts
        would read as decisions taken (an optional slot's confident `none` as
        a decline). They are dropped, not relabelled."""
        unused = {idx for _d, _v, idx in self._prefetched.values()}
        self._prefetched = {}
        return [r for i, r in enumerate(records) if i not in unused]

    def _readings(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> SemanticModel:
        """Every ambiguous term the question triggers, settled before any slot
        is decided: the lexical rail first (a question-named or audience-
        resolved reading pins for free), then one typed decision over the
        declared readings (reading.py) for the residue — act pins, none or a
        stray name clarifies. The pinned reading rides the entity and metric
        decisions as a note. The pipeline pins the same way before the
        generator; a pinned definition is active here and is left alone."""
        settled = ambiguity.resolve(question, model)
        pinned = list(settled)
        for d in model.definitions:
            if d.status != "ambiguous" or len(d.possible_mappings) < 2:
                continue
            if not ambiguity.triggered(question, d) or any(r.term == d.term for r in settled):
                continue
            picked, record = reading.decide_reading(self._decider, question, d, self._policies)
            records.append(record)
            if picked is None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="ambiguous_term",
                        term=d.term,
                        question=f"'{d.term}' has more than one meaning here — which do you mean? "
                        + "; ".join(d.possible_mappings),
                        options=list(d.possible_mappings),
                    )
                )
            pinned.append(picked)
        if not pinned:
            return model
        # Word for word the pipeline's context chunk: the same fact must read
        # the same on every rail, or the decisions it steers drift between them.
        notes = " ".join(
            f"GOVERNED RESOLUTION — the ambiguous term '{r.term}' is pinned for this "
            f"question to: {r.mapping} ({r.via}). Compute that reading; do not ask for "
            "clarification."
            for r in pinned
        )
        self._notes = f"{self._notes} {notes}".strip()
        return ambiguity.pin(model, pinned)

    def _resolve(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> tuple[QueryIntent, list[str]]:
        model = self._readings(question, model, records)
        sets = option_sets(model, [], domains=self._domains)
        entity_options = [
            Option(e.name, " ".join(p for p in (e.description or "", e.grain or "") if p))
            for e in model.entities
        ]

        shape_ctx = "What kind of answer does the question ask for?"
        entity_ctx = self._noted(
            "Which entity (table of the semantic model) is the question about?"
        )
        metric_ctx = self._noted("Which governed metric does the question ask for?")
        # The entity a metric option belongs to: qualified names say it, a bare
        # name has one owner in the lens.
        owner_of: dict[str, str] = {}
        for e in model.entities:
            for m in e.metrics:
                owner_of.setdefault(m.name, e.name)
                owner_of[f"{e.name}.{m.name}"] = e.name
        # Round 1: what is asked, WHICH METRIC over every metric of the lens
        # (the metric names the entity — deciding the entity first lost to a
        # sibling entity on every distinctive metric name), and the entity for
        # a listing — independent, one call.
        self._fan_out(
            question,
            {
                "shape": ("shape", shape_ctx, _SHAPES, True),
                **(
                    {"metric:0": ("metric", metric_ctx, sets["metric"], False)}
                    if sets["metric"]
                    else {}
                ),
                **(
                    {"entity": ("entity", entity_ctx, entity_options, True)}
                    if len(model.entities) > 1
                    else {}
                ),
            },
            records,
        )
        shape = self._pick(
            "shape",
            question,
            shape_ctx,
            _SHAPES,
            allow_none=True,
            records=records,
            clarify_term="shape",
            clarify_question="Is this asking for a figure or for a list of records?",
            decline_reason="not a question this lens's data answers (no figure, no listing)",
            key="shape",
        )
        if shape is None:
            raise _Decline("not a question this lens's data answers")

        first_metric: str | None = None
        if shape == "aggregate":
            if not sets["metric"]:
                raise _Decline("no governed metric of this lens answers the question")
            first_metric = self._pick(
                "metric",
                question,
                metric_ctx,
                sets["metric"],
                allow_none=False,
                records=records,
                clarify_term="metric",
                clarify_question="Which metric does the question mean? Candidates: "
                + ", ".join(o.name for o in sets["metric"][:6])
                + ".",
                key="metric:0",
            )
            if first_metric is None:
                raise _Decline("no governed metric of this lens answers the question")
            entity_name: str | None = owner_of.get(first_metric)
        else:
            entity_name = (
                model.entities[0].name
                if len(model.entities) == 1
                else self._pick(
                    "entity",
                    question,
                    entity_ctx,
                    entity_options,
                    allow_none=True,
                    records=records,
                    clarify_term="entity",
                    clarify_question="Which entity is this about? "
                    + ", ".join(e.name for e in model.entities),
                    decline_reason="no entity of this lens holds what the question asks about",
                    key="entity",
                )
            )
        if entity_name is None:
            raise _Decline("no entity of this lens holds what the question asks about")
        entity = next(e for e in model.entities if e.name == entity_name)
        own_metrics = [
            o
            for o in sets["metric"]
            if o.name.rsplit(".", 1)[-1] in {m.name for m in entity.metrics}
        ]
        own_members = [
            *(Option(d.name, d.description or "") for d in entity.dimensions),
            *(
                Option(f.name, f.description or "")
                for f in entity.fields
                if f.name not in {d.name for d in entity.dimensions}
            ),
        ]

        # Round 2: every pick that depends only on the entity — the second
        # metric (or the first listing field), the first dimension, the grain,
        # each definition, the first filter column — one call; round three and
        # the sequential steps follow.
        fields_ctx = "Which fields should each listed row show?"
        dims = [o for o in own_members if o.name in {d.name for d in entity.dimensions}]
        dim_ctx = "Is the figure broken down by a dimension (per X / by Y)?"
        grain_ctx = (
            "The question asks for the figure broken down over time. Which period is "
            "each row — hour, day, week, month, quarter or year? A stated period such "
            "as 'in August 2026' is the window, not the breakdown."
        )
        over_time = asks_breakdown_over_time(question)
        grain_opts = [*sets["grain"], Option(_NONE, "no time series is asked for")]
        filter_candidates = self._filter_candidates(question, entity, own_members)
        round_two: dict[str, tuple[str, str, list[Option], bool]] = {}
        if shape == "aggregate":
            rest_metrics = [o for o in own_metrics if o.name != first_metric]
            if rest_metrics and MAX_METRICS > 1 and first_metric is not None:
                round_two["metric:1"] = (
                    "metric",
                    self._another(metric_ctx, [first_metric]),
                    [*rest_metrics, _ANOTHER_NONE],
                    True,
                )
            round_two["dimension:0"] = ("dimension", dim_ctx, [*dims, _ANOTHER_NONE], True)
            if entity.default_time_field and over_time:
                round_two["grain"] = ("grain", grain_ctx, grain_opts, True)
        else:
            round_two["field:0"] = ("dimension", fields_ctx, own_members, False)
        # A definition is asked ("does the question engage it?") only when
        # the question carries its surface words (term or aliases): under
        # act-unless-none an "engaged" on a question that never named the
        # term would apply a filter nobody asked for — a silent-wrong on every
        # question for every definition. Aliases are how authoring widens it.
        for d in model.definitions:
            if ambiguity.triggered(question, d) and (d.sql_expr or "").strip():
                round_two[f"definition:{d.term}"] = (
                    "definition",
                    self._definition_ctx(d),
                    _ENGAGED,
                    False,
                )
        round_two["filter:0"] = (
            "filter",
            self._filter_ctx([]),
            [*filter_candidates, Option(_NONE, "no further restriction is asked for")],
            True,
        )
        self._fan_out(question, round_two, records)

        # Round 3: everything that depends only on the first picks — the
        # second metric / dimension / field ("another, or none"), the first
        # filter column's value (or whether a value is stated at all), the
        # second filter column — one call. A question that needs a third of
        # anything falls to the sequential steps; most never do.
        round_three: dict[str, tuple[str, str, list[Option], bool]] = {}
        second_metric = self._peek("metric:1")
        if first_metric is not None and second_metric is not None:
            rest = [o for o in own_metrics if o.name not in (first_metric, second_metric)]
            if rest and MAX_METRICS > 2:
                round_three["metric:2"] = (
                    "metric",
                    self._another(metric_ctx, [first_metric, second_metric]),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        first_dim = self._peek("dimension:0")
        if first_dim is not None:
            rest = [o for o in dims if o.name != first_dim]
            if rest and MAX_DIMENSIONS > 1:
                round_three["dimension:1"] = (
                    "dimension",
                    self._another(dim_ctx, [first_dim]),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        first_field = self._peek("field:0")
        if first_field is not None:
            rest = [o for o in own_members if o.name != first_field]
            if rest:
                round_three["field:1"] = (
                    "dimension",
                    self._another(fields_ctx, [first_field]),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        first_filter = self._peek("filter:0")
        if first_filter is not None:
            spec = self._value_spec(question, entity, first_filter)
            if spec is not None:
                round_three[spec[0]] = spec[1]
            rest = [o for o in filter_candidates if o.name != first_filter]
            if rest and MAX_FILTERS > 1:
                round_three["filter:1"] = (
                    "filter",
                    self._filter_ctx([first_filter]),
                    [*rest, Option(_NONE, "no further restriction is asked for")],
                    True,
                )
        self._fan_out(question, round_three, records)

        metrics: list[str] = []
        fields: list[str] = []
        if shape == "aggregate":
            metrics = [
                m.rsplit(".", 1)[-1]
                for m in self._pick_many(
                    "metric",
                    question,
                    metric_ctx,
                    own_metrics,
                    cap=MAX_METRICS,
                    records=records,
                    clarify_term="metric",
                    first_required=True,
                    key_prefix="metric",
                    initial=[first_metric] if first_metric is not None else None,
                )
            ]
            if not metrics:
                raise _Decline("no governed metric of this lens answers the question")
        else:
            fields = self._pick_many(
                "dimension",
                question,
                fields_ctx,
                own_members,
                cap=6,
                records=records,
                clarify_term="field",
                first_required=True,
                key_prefix="field",
            )
            if not fields:
                raise _Decline("no field of this lens matches what the listing asks for")

        dimensions: list[str] = []
        if shape == "aggregate":
            dimensions = self._pick_many(
                "dimension",
                question,
                dim_ctx,
                dims,
                cap=MAX_DIMENSIONS,
                records=records,
                clarify_term="dimension",
                first_required=False,
                key_prefix="dimension",
            )

        grain: TimeGrain | None = None
        if shape == "aggregate" and entity.default_time_field and over_time:
            g = self._pick(
                "grain",
                question,
                grain_ctx,
                grain_opts,
                allow_none=True,
                records=records,
                clarify_term="grain",
                clarify_question="Over which period should the figure be broken down — "
                "hour, day, week, month, quarter, year, or none?",
                key="grain",
            )
            grain = g if g in {o.name for o in sets["grain"]} else None  # type: ignore[assignment]

        definitions = self._definitions(question, model, records)
        filters, supplied = self._filters(question, entity, own_members, records)
        filters += self._window(question, entity, metrics)
        order_by, limit = self._order(question, metrics, fields)

        intent = QueryIntent(
            entity=entity.name,
            metrics=metrics,
            dimensions=dimensions,
            fields=fields,
            definitions=definitions,
            grain=grain,
            filters=filters,
            order_by=order_by,
            limit=limit,
        )
        return intent, supplied

    def _noted(self, context: str) -> str:
        return f"{context} {self._notes}".strip() if getattr(self, "_notes", "") else context

    @staticmethod
    def _definition_ctx(d: Definition) -> str:
        return (
            f"The governed term '{d.term}' means: {d.body}. Does the question engage "
            "that meaning (should its filter apply)?"
        )

    def _filter_candidates(
        self, question: str, entity: Entity, members: list[Option]
    ) -> list[Option]:
        """The fields a filter may restrict by: never the entity's time field,
        and when the question states a window no date-typed field and no
        profiled month-period field either — the deterministic window owns
        time (rule 5), so "in August 2026" is a range on the time field, not
        a `due_date` or `month` value the agent must bind."""
        time_field = (entity.default_time_field or "").lower()
        windowed = bool(timewindow.temporal_terms(question))
        period = (self._period_field(entity) or "").lower() if windowed else ""
        out: list[Option] = []
        for m in members:
            if m.name.lower() in (time_field, period) and m.name.lower():
                continue
            if windowed and _field_type(entity, m.name) in ("date", "timestamp"):
                continue
            out.append(m)
        return out

    @staticmethod
    def _filter_ctx(columns: list[str]) -> str:
        return (
            "Does the question restrict the rows by a field's value (a status, a "
            "region, a customer, an amount)? Pick the field, or none."
            + (f" Already applied: {', '.join(columns)}." if columns else "")
        )

    def _definitions(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> list[str]:
        out: list[str] = []
        resolved = {r.term for r in ambiguity.resolve(question, model)}
        for d in model.definitions:
            if not ambiguity.triggered(question, d):
                continue
            if d.status == "ambiguous" and len(d.possible_mappings) >= 2 and d.term not in resolved:
                raise _Clarify(
                    ClarificationRequest(
                        kind="ambiguous_term",
                        term=d.term,
                        question=f"'{d.term}' has more than one meaning here — which do you mean? "
                        + "; ".join(d.possible_mappings),
                        options=list(d.possible_mappings),
                    )
                )
            if not (d.sql_expr or "").strip():
                continue
            pick = self._pick(
                "definition",
                question,
                self._definition_ctx(d),
                _ENGAGED,
                allow_none=False,
                records=records,
                clarify_term=d.term,
                clarify_question=f"Should the governed meaning of '{d.term}' apply here?",
                key=f"definition:{d.term}",
            )
            if pick == "engaged":
                out.append(d.term)
        return out

    def _filters(
        self,
        question: str,
        entity: Entity,
        members: list[Option],
        records: list[DecisionRecord],
    ) -> tuple[list[IntentFilter], list[str]]:
        filters: list[IntentFilter] = []
        supplied: list[str] = []
        candidates = self._filter_candidates(question, entity, members)
        # Column, then its value, then "another filter or none" — one filter at a
        # time, so the value decision follows the column it belongs to.
        columns: list[str] = []
        while candidates and len(columns) < MAX_FILTERS:
            column = self._pick(
                "filter",
                question,
                self._filter_ctx(columns),
                [*candidates, Option(_NONE, "no further restriction is asked for")],
                allow_none=True,
                records=records,
                clarify_term="filter",
                clarify_question="Which field should the answer be restricted by? "
                + ", ".join(o.name for o in candidates[:6]),
                key=f"filter:{len(columns)}",
            )
            if column is None:
                break
            columns.append(column)
            candidates = [o for o in candidates if o.name != column]
            self._value(question, entity, column, filters, supplied, records)
        return filters, supplied

    @staticmethod
    def _value_options(domain: list[str]) -> list[Option]:
        return [
            *(Option(v) for v in domain),
            Option(_UNSTATED, "the question states no value for this field"),
            Option(_NONE, "the question states a value, but not one of these"),
        ]

    def _value_spec(
        self, question: str, entity: Entity, column: str
    ) -> tuple[str, tuple[str, str, list[Option], bool]] | None:
        """The value decision a filter column needs, as a fan-out spec — the
        same question ``_value`` asks, so a round may ask it early. None when
        no decision is needed (a binding, a literal in the question)."""
        ftype = _field_type(entity, column)
        domain = self._domains_for(entity).get(column.lower())
        bound = self._bindings.get(column.lower())
        if domain:
            if bound is not None and bound in domain:
                return None
            return (
                f"filter_value:{column}",
                (
                    "filter_value",
                    f"Which stored value of '{column}' does the question mean?",
                    self._value_options(domain),
                    True,
                ),
            )
        if bound is not None or self._literal_in_question(question, ftype) is not None:
            return None
        return (
            f"stated:{column}",
            (
                "filter_value",
                f"Does the question state a value for '{column}' to filter on?",
                _STATED,
                False,
            ),
        )

    def _value(
        self,
        question: str,
        entity: Entity,
        column: str,
        filters: list[IntentFilter],
        supplied: list[str],
        records: list[DecisionRecord],
    ) -> None:
        ftype = _field_type(entity, column)
        domain = self._domains_for(entity).get(column.lower())
        bound = self._bindings.get(column.lower())
        if domain:
            if bound is not None and bound in domain:
                # The caller bound it: the value is theirs, no decision to make.
                filters.append(IntentFilter(field=column, op="=", value=bound))
                supplied.append(column)
                return
            value = self._pick(
                "filter_value",
                question,
                f"Which stored value of '{column}' does the question mean?",
                self._value_options(domain),
                allow_none=True,
                records=records,
                clarify_term=column,
                clarify_question=f"Which value of '{column}'? It holds: {', '.join(domain)}.",
                key=f"filter_value:{column}",
            )
            if value == _UNSTATED:
                return  # not a filter after all — dropped, on the record
            if value is None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unknown_value",
                        term=column,
                        question=f"The question's value for '{column}' is not one it holds. "
                        f"It holds: {', '.join(domain)}.",
                        options=list(domain),
                    )
                )
            filters.append(IntentFilter(field=column, op="=", value=value))
            return
        if bound is not None:
            value_obj = self._typed_binding(column, ftype, bound)
            op = (
                self._op(question, column, records)
                if ftype in ("number", "integer", "date", "timestamp")
                else "="
            )
            filters.append(IntentFilter(field=column, op=op, value=value_obj))
            supplied.append(column)
            return
        literal = self._literal_in_question(question, ftype)
        if literal is not None:
            op = self._op(question, column, records)
            filters.append(IntentFilter(field=column, op=op, value=literal))
            return
        # No dictionary, no binding, no literal: one more decision before asking
        # the agent to bind — does the question state a value at all? A column
        # picked at low p on a question that names no value is not a filter.
        stated = self._pick(
            "filter_value",
            question,
            f"Does the question state a value for '{column}' to filter on?",
            _STATED,
            allow_none=False,
            records=records,
            clarify_term=column,
            clarify_question=f"Which value of '{column}' should the answer filter on?",
            key=f"stated:{column}",
        )
        if stated == _UNSTATED:
            return
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=column,
                question=f"Which value of '{column}' should the answer filter on? "
                f"Supply it as a binding ({_grammar_for(ftype)}).",
                options=[_grammar_for(ftype)],
            )
        )

    def _op(self, question: str, column: str, records: list[DecisionRecord]) -> FilterOp:
        pick = self._pick(
            "filter_op",
            question,
            f"How does the question compare '{column}' to the value?",
            _OPS,
            allow_none=False,
            records=records,
            clarify_term=column,
            clarify_question=f"Should '{column}' be equal to, above, or below the value?",
        )
        return cast(FilterOp, pick or "=")

    @staticmethod
    def _typed_binding(column: str, ftype: str | None, raw: str) -> object:
        raw = raw.strip()
        if ftype in ("number", "integer"):
            if not re.fullmatch(r"-?\d+(\.\d+)?", raw):
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"The binding for '{column}' must be a number, got {raw!r}.",
                        options=[GRAMMAR["number"]],
                    )
                )
            return float(raw) if "." in raw else int(raw)
        if ftype in ("date", "timestamp"):
            try:
                date.fromisoformat(raw)
            except ValueError as exc:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"The binding for '{column}' must be YYYY-MM-DD, got {raw!r}.",
                        options=[GRAMMAR["date"]],
                    )
                ) from exc
            return raw
        return raw

    @staticmethod
    def _literal_in_question(question: str, ftype: str | None) -> object | None:
        """A number or ISO date resolves from the question only when exactly one
        standalone literal of that type appears — one is a fact, two is a guess."""
        if ftype in ("number", "integer"):
            stripped = _ISO_DATE.sub(" ", _YEARISH.sub(" ", question))
            nums = _NUMBER.findall(stripped)
            if len(nums) == 1:
                return float(nums[0]) if "." in nums[0] else int(nums[0])
            return None
        if ftype in ("date", "timestamp"):
            dates = _ISO_DATE.findall(question)
            return dates[0] if len(dates) == 1 else None
        return None

    def _window(self, question: str, entity: Entity, metrics: list[str]) -> list[IntentFilter]:
        terms = timewindow.temporal_terms(question)
        # A month the question names that no window term carries would be
        # served as the OTHER months alone (or as all time) — a silently wrong
        # period. Ask for the period instead; never drop a stated month.
        if unplaced := timewindow.unplaced_months(question, terms):
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="window",
                    question="Which period exactly? The question names "
                    f"{', '.join(unplaced)} in a way the window could not place — "
                    f"supply the period as a binding ({GRAMMAR['date_range']}).",
                    options=[GRAMMAR["date_range"]],
                )
            )
        if not terms:
            return []
        field_name = entity.default_time_field
        for m in entity.metrics:
            if m.name in metrics and m.agg_time_field:
                field_name = m.agg_time_field
        period = None if field_name else self._period_field(entity)
        if period is not None:
            field_name = period
        if not field_name:
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="window",
                    question="The question states a time window, but this entity declares no "
                    "time field to apply it to.",
                    options=[GRAMMAR["date_range"]],
                )
            )
        ranges = timewindow.window_ranges(terms, self._today)
        if len(ranges) != 1:
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term=field_name,
                    question="Which period exactly? Supply it as a binding for "
                    f"'{field_name}' ({GRAMMAR['date_range']}).",
                    options=[GRAMMAR["date_range"]],
                )
            )
        start, end = ranges[0]
        if period is not None:
            # The profiled month form: one month is an equality (the SQL a
            # human certifies reads `month = '2026-08'`), a span is bound
            # inclusively on both ends.
            first, last = start.strftime("%Y-%m"), end.strftime("%Y-%m")
            if first == last:
                return [IntentFilter(field=field_name, op="=", value=first)]
            return [
                IntentFilter(field=field_name, op=">=", value=first),
                IntentFilter(field=field_name, op="<=", value=last),
            ]
        return [
            IntentFilter(field=field_name, op=">=", value=start.isoformat()),
            IntentFilter(field=field_name, op="<=", value=end.isoformat()),
        ]

    @staticmethod
    def _order(
        question: str, metrics: list[str], fields: list[str]
    ) -> tuple[list[IntentOrder], int | None]:
        limit: int | None = None
        direction: Literal["asc", "desc"] | None = None
        if m := _TOP.search(question):
            limit, direction = int(m.group(1)), "desc"
        elif m := _BOTTOM.search(question):
            limit, direction = int(m.group(1)), "asc"
        elif _HIGHEST.search(question):
            direction = "desc"
        elif _LOWEST.search(question):
            direction = "asc"
        if direction is None or not metrics:
            return [], limit
        return [IntentOrder(field=metrics[0], dir=direction)], limit


class TypedIntentGenerator:
    """The generator seam over the typed resolver: act on every slot → compiled
    SQL; a clarify → the clarification; a decline → the named gap. Consumes no
    generation tokens of its own — the decisions are the cost. Repair feedback
    is ignored on purpose: the same question decides the same way."""

    def __init__(self, resolver: TypedResolver) -> None:
        self._resolver = resolver
        self.model = "typed"

    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery:
        notes = [c.text for c in prose_context if c.source.startswith("ambiguity-resolution:")]
        res = self._resolver.resolve(question, semantic_model, notes=notes)
        if res.clarification is not None:
            return GeneratedQuery(sql="", clarification=res.clarification, decisions=res.decisions)
        if res.intent is None:
            return GeneratedQuery(
                sql="", no_answer_reason=res.decline or "did not type", decisions=res.decisions
            )
        try:
            sql = compile_intent(res.intent, semantic_model)
        except CompileError as exc:
            return GeneratedQuery(
                sql="",
                no_answer_reason=f"the typed intent did not compile: {exc}",
                decisions=res.decisions,
            )
        return GeneratedQuery(
            sql=sql,
            definition_used=intent_term(res.intent),
            intent=res.intent,
            decisions=res.decisions,
            supplied=res.supplied,
            typed=True,
        )
