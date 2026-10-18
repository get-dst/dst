"""Metric-layer query generator — emit a structured QueryIntent, compile it to SQL.

Implements the same `QueryGenerator` seam as GroundedSQLGenerator, so it drops into the
pipeline unchanged. The LLM picks metrics/dimensions/filters by name from the model; the
compiler turns that into correct SQL deterministically, resolving join paths and refusing
the ones that would fan the metrics out. If the model can't form a valid intent (or it
won't compile), it returns empty SQL, which the guard rejects and the pipeline escalates
to raw-SQL generation.
"""

from __future__ import annotations

import json
import re

from services.contracts.protocols import (
    CacheableBlock,
    ContextChunk,
    GeneratedQuery,
    LLMProvider,
    Message,
)
from services.contracts.query_intent import QueryIntent
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.runtime import ambiguity
from services.runtime.compiler import (
    CompileError,
    compile_intent,
    govern_literals,
    prefer_weighted_ratio,
)
from services.runtime.timewindow import breakdown_unit

_RULES = (
    "Translate the question into a structured QUERY INTENT over the lens's metric layer.\n"
    "Pick metrics and dimensions ONLY by the names listed below; filters reference field "
    "names. Do not invent names. If the question needs something not listed (e.g. a metric "
    "that doesn't exist), return empty metrics and dimensions.\n"
    "`entity` is the entity the METRICS belong to. Dimensions and filters may name members "
    "of ANY entity — the compiler resolves the join path itself. Write a dimension as "
    "`entity.member` when the same name appears on more than one entity.\n"
    "For an over-time question ('by month', 'per week', 'daily trend'), set `grain` to "
    "hour|day|week|month|quarter|year and do NOT also list the time field as a dimension — "
    "the compiler buckets the entity's time field for you. A level metric (marked "
    "[level]) asked 'by month', 'per month', 'each month' or 'at month end per month' is "
    "a series of one snapshot per bucket, never one latest row: set `grain` (or group by "
    "the entity's own month/week dimension).\n"
    "When a listed definition carries a `[filter: ...]`, that filter IS the governed "
    "meaning of the term: put the term in `definitions` and the compiler applies the "
    "predicate for you — do NOT reconstruct the condition yourself in `filters`. List a "
    "definition ONLY when the question names the term, one of its aliases, or the metric "
    "it is about; a definition the question never names is not applied.\n"
    "A time window ('in September') is a filter on the time field. When the question "
    "asks for metrics that read different time fields, bound ONE of those fields: the "
    "compiler applies the window to each metric on its own time field.\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"entity": "<name or null>", "metrics": ["..."], "dimensions": ["..."], '
    '"definitions": ["..."], '
    '"filters": [{"field": "<field>", "op": "=", "value": <value>}], '
    '"order_by": [{"field": "<metric or dimension>", "dir": "desc"}], "limit": null}'
)


def serialize_layer(model: SemanticModel) -> str:
    from services.runtime.generator import business_today

    lines = [f"Dialect: {model.dialect}"]
    # The DEFAULT door (any lens with metrics) states the date: without it
    # 'this year' compiles to the model's own training-cutoff prior, and no
    # authoring lever reaches this pass. Same line as the raw-SQL prompt.
    lines.append(
        f"Today: {business_today(model.timezone)} — resolve 'this year', 'last "
        f"month', 'this quarter' against THIS date, never against your own prior. "
        f"For 'now'/'current' against a snapshot or status-date column, filter to "
        f"the LATEST AVAILABLE date (col = (SELECT MAX(col) FROM …)), never to "
        f"today's literal date — snapshots lag, and today's literal matches nothing."
    )
    if model.timezone:
        # The business clock rides next to the dialect because it is the same kind
        # of fact: one every generated query must agree on. "Yesterday" against a
        # UTC event stream under an Oslo team moves the answer by up to the
        # offset's worth of traffic.
        lines.append(
            f"Timezone: {model.timezone} — 'today', 'yesterday' and period "
            f"boundaries are this zone's, unless the question names another."
        )
    if model.stale_after_days:
        lines.append(
            f"Freshness contract: data is considered stale {model.stale_after_days} days after "
            f"its last update; the service measures and reports actual freshness — never "
            f"assert the data is current."
        )
    lines += ["", "Building blocks (use ONLY these names):"]
    for e in model.entities:
        lines.append(f"Entity '{e.name}':")
        if e.exposed:
            lines.append(
                "  no governed definitions — an exposed table: raw columns only, no "
                "metric, dimension or definition of this lens applies to it"
            )
        if e.grain:
            lines.append(f"  Grain: {e.grain}")
        if e.population:
            # Without this, a scoped table answers "all X" questions as if it
            # held the whole population — the bound rides both prompts, the
            # compiler, and the serve check, the same rails dialect rides.
            lines.append(
                f"  Population: this table holds ONLY {e.population} — every answer "
                "must state this scope, and questions about 'all'/'total' beyond it "
                "cannot be answered from this table."
            )
        if e.population_filter:
            lines.append(f"  Required filter on every query: {e.population_filter}")
        if e.pinned_dimensions:
            dims = ", ".join(e.pinned_dimensions)
            lines.append(
                f"  Pinned dimensions: {dims} — every aggregate over this entity must "
                "filter these to one value or group by them; a SUM across them is a "
                "number denominated in nothing."
            )
        if e.default_time_field:
            # `grain` buckets this field; grouping on it raw is per-event grain wearing
            # a per-period costume, which is the whole of what time_guard catches.
            lines.append(f"  Time field: {e.default_time_field} (set `grain` for trends)")
        for u in e.use_cases:
            lines.append(f"  Use: {u}")
        if e.metrics:
            lines.append("  Metrics:")
            for m in e.metrics:
                desc = f" — {m.description}" if m.description else ""
                filters = f" [only where {' AND '.join(m.filters)}]" if m.filters else ""
                if m.type == "ratio":
                    head = f"ratio {m.numerator} / {m.denominator}"
                    if m.period_weight:
                        head += (
                            f" x {m.period_weight} in the period [select THIS metric for the "
                            "ratio, with a bounded window — never its two operands]"
                        )
                elif m.type == "derived":
                    head = f"derived {m.expr}"
                else:
                    head = f"{m.agg} of {m.expr}"
                if m.additivity == "level":
                    head += " [level: a snapshot — a period's figure is its last snapshot]"
                if m.agg_time_field:
                    head += f" [time field: {m.agg_time_field}]"
                lines.append(f"    - {m.name}: {head}{filters}{desc}")
        lines.append("  Dimensions & fields (group / filter / order):")
        for d in e.dimensions:
            desc = f" — {d.description}" if d.description else ""
            lines.append(f"    - {d.name}{desc}")
        for f in e.fields:
            # The description rides HERE for the same reason `sql_expr` does above:
            # authored meaning that never reaches the prompt cannot change an answer.
            # A field's description is where authors put the VALUE DOMAIN — the
            # scaffold's own example is `country: "ISO country code (NO, SE, DK, FI,
            # IS)"` — and the raw-SQL prompt has always rendered it (generator.py).
            # This tier dropped it, so on the DEFAULT door (any lens with metrics) a
            # filter on a coded column was written from the question's vocabulary
            # instead of the column's: "customers in Denmark and Finland" compiled to
            # `country IN ('Denmark','Finland')` against a column holding 'DK'/'FI'
            # and returned zero rows — confidently, with no signal.
            desc = f" — {f.description}" if f.description else ""
            lines.append(f"    - {f.name} ({f.type}){desc}")
    if model.joins:
        # Without this the model cannot tell a reachable dimension from an unrelated one,
        # and the compiler's join resolution is invisible to the thing writing the intent.
        lines += ["", "Entities you may combine (dimensions/filters can cross these):"]
        lines += [f"- {j.left} <-> {j.right}" for j in model.joins]
    if model.definitions:
        lines += ["", "Definitions (list a term in `definitions` to apply its governed filter):"]
        for defn in model.definitions:
            # The enforceable half rides HERE now, not only on the raw-SQL prompt: a
            # metric lens whose author edits `status_code = 'X'` to `'C'` must be able
            # to change the answer, and it can only do that if the model can name the
            # definition and the compiler injects the current predicate.
            applied = f"  [filter: {defn.sql_expr}]" if (defn.sql_expr or "").strip() else ""
            lines.append(f"- {defn.term}: {defn.body}{applied}")
    # `ai_instructions` is deliberately NOT rendered here: rendering it does not
    # change the SQL this pass compiles. That is a grammar gap, not a knowledge
    # gap — an author's rulings are things like "list DISTINCT" and "project
    # only the column asked for, not the count it was ranked by", and
    # QueryIntent cannot express either; its only lever is the escape hatch
    # (empty metrics + dimensions -> escalate), so stating the rule here just
    # makes the pass bail marginally more often. The rulings land on the
    # raw-SQL prompt, which can act on them. What a metric lens loses here is
    # not silent: `runtime.preview.escalation_only` names it and apply warns
    # with it.
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    return t


def parse_intent(text: str) -> QueryIntent | None:
    try:
        return QueryIntent.model_validate(json.loads(_strip_fences(text)))
    except Exception:
        return None


def gate_definitions(question: str, intent: QueryIntent, model: SemanticModel) -> QueryIntent:
    """The intent with every model-chosen definition the question never names
    (term, alias, or the member it is `about`, as literal words) moved from
    ``definitions`` to ``definitions_suggested``. The compiler applies only the
    named ones; the suggested ones ride the resolution as a fact about the
    answer. A predicate the caller never asked for is a silent filter."""
    by_term = {d.term: d for d in model.definitions}
    named = [
        t for t in intent.definitions if t not in by_term or ambiguity.named(question, by_term[t])
    ]
    suggested = [t for t in intent.definitions if t not in named]
    # A named definition with no sql_expr is glossary: it contributes no
    # predicate, and handing it to the compiler fails the whole intent to the
    # raw-SQL tier over nothing (the metric 'utilisation' also named as the
    # term 'planned_capacity_slots' lost its compiled ratio that way). It is
    # dropped here; the definition's prose still reaches the answer's basis.
    named = [t for t in named if t not in by_term or (by_term[t].sql_expr or "").strip()]
    # The model names the metric it selected as a definition too ("utilisation"
    # in both lists): a metric is not a term, and an unknown term failed the
    # whole intent to the raw-SQL tier over nothing.
    metric_names = {m.name for e in model.entities for m in e.metrics}
    named = [t for t in named if t in by_term or t not in metric_names]
    if not suggested and named == intent.definitions:
        return intent
    return intent.model_copy(
        update={
            "definitions": named,
            "definitions_suggested": [*intent.definitions_suggested, *suggested],
        }
    )


def gate_grain(question: str, intent: QueryIntent, model: SemanticModel) -> QueryIntent:
    """The intent with ``grain`` pinned to the question's breakdown phrase ("by
    month", "monthly", "one row per month") and the entity's own date-typed
    dimensions removed beside it. The phrase is deterministic, so the asked
    period is a rail and not the model's call, for a flow and a level alike:
    a model that keeps ``day`` as a dimension next to ``grain: month`` compiles
    ``GROUP BY DATE_TRUNC('MONTH', day), day`` — the bucket is a no-op and a
    "monthly" answer serves every day. A level pinned to one latest snapshot
    under a "per month" ask collapses the series to its last day the same
    way; once the grain says so the compiler reads one snapshot per bucket.
    A question naming no unit leaves the model's grain as it set it."""
    unit = breakdown_unit(question)
    grain = unit or intent.grain
    if grain is None or not intent.metrics:
        return intent
    entity = next((e for e in model.entities if e.name == intent.entity), None)
    if entity is None:
        return intent
    by_name = {m.name: m for m in entity.metrics}
    if all(getattr(by_name.get(m), "period_weight", None) for m in intent.metrics):
        # A period-weighted ratio reads one window: its weight IS the period,
        # and "slot capacity per week" in the question is the formula's unit,
        # not a breakdown ask. The compiler refuses a grain on it either way.
        return intent
    typed = {d.name: d.type for d in entity.dimensions} | {f.name: f.type for f in entity.fields}
    dimensions = [
        d for d in intent.dimensions if typed.get(d.rpartition(".")[2]) not in ("date", "timestamp")
    ]
    if grain == intent.grain and dimensions == intent.dimensions:
        return intent
    return intent.model_copy(update={"grain": grain, "dimensions": dimensions})


def intent_term(intent: QueryIntent | None) -> str | None:
    """The compiler-validated term a compiled intent implements — basis provenance.

    Exactly one metric names it outright; no metrics but exactly one governed
    definition names that. Anything plural returns None: naming ONE of several
    would be the wrong-provenance class this exists to kill, and the caller
    falls back to reading the served SQL."""
    if intent is None:
        return None
    if len(intent.metrics) == 1:
        return intent.metrics[0]
    if not intent.metrics and len(intent.definitions) == 1:
        return intent.definitions[0]
    return None


class IntentSQLGenerator:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        model: str = "claude-haiku-4-5",
        temperature: float = 0.0,
        # Same reasoning-model starvation as GroundedSQLGenerator: intents are
        # small, but the thinking that precedes them is not.
        max_tokens: int = 4096,
    ) -> None:
        self._llm = llm
        self.model = model  # public: the pipeline reads it to attribute cost per call
        self._temperature = temperature
        self._max_tokens = max_tokens

    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery:
        system = CacheableBlock(_RULES + "\n\n" + serialize_layer(semantic_model), ttl="1h")
        parts: list[str] = []
        if prose_context:
            prose = "\n\n".join(f"[{c.source}] {c.text}" for c in prose_context)
            parts.append(f"Context:\n{prose}")
        parts.append(f"Question: {question}")
        if feedback:
            parts.append(f"Your previous attempt failed and must be corrected. {feedback}")
        messages = [Message("user", "\n\n".join(parts))]
        in_tok = out_tok = 0
        intent: QueryIntent | None = None
        sql = ""
        error: CompileError | None = None
        # The compiler's refusal is feedback the model can act on (a window on
        # the wrong field, a grain two metrics cannot share): one repair round
        # before the empty SQL hands the question to the next tier.
        for _attempt in range(2):
            res = self._llm.complete(
                system=[system],
                messages=messages,
                model=self.model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            in_tok += res.input_tokens
            out_tok += res.output_tokens
            intent = parse_intent(res.text)
            if intent is None:
                break
            intent = gate_definitions(question, intent, semantic_model)
            # The ratio rewrite runs before the grain gate: once the pair is
            # the period-weighted ratio, "per week" in the question is its
            # formula's unit and the gate leaves it alone.
            intent = prefer_weighted_ratio(intent, semantic_model)
            intent = gate_grain(question, intent, semantic_model)
            intent = govern_literals(intent, semantic_model)
            try:
                sql = compile_intent(intent, semantic_model)
                error = None
                break
            except CompileError as exc:
                error = exc
                sql = ""  # guard will reject empty SQL → pipeline escalates to raw SQL
                messages.append(Message("assistant", res.text))
                messages.append(
                    Message(
                        "user",
                        f"That intent did not compile: {exc}. Return a corrected intent "
                        "using only the listed names.",
                    )
                )
        # The compiler resolved these names or raised — they ARE the provenance.
        # Discarding them makes basis fall back to substring attribution, which
        # ties on sibling metrics and cites the wrong one.
        gq = GeneratedQuery(
            sql=sql,
            definition_used=intent_term(intent) if sql else None,
            intent=intent if sql else None,
        )
        if error is not None and "ask for them separately" in str(error):
            # Two readings the compiler cannot put in one query, and the repair
            # did not split them: ask, rather than let a raw tier guess.
            gq.clarification = ClarificationRequest(
                kind="unresolved_slot",
                term="metric",
                question=f"These cannot be computed in one query: {error}",
                options=list(intent.metrics) if intent is not None else [],
            )
        gq.input_tokens = in_tok
        gq.output_tokens = out_tok
        return gq
