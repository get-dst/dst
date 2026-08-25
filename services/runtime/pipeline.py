"""Runtime pipeline: ground -> generate -> guard -> execute -> compose.

Pure orchestration (no DB) so it is fully testable with fakes. Produces both the
caller `QueryResponse` and the full `TraceLog`; persisting the trace is the logger's
job, called by the REST/MCP layer.
"""

from __future__ import annotations

import datetime
import decimal
import functools
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

import sqlglot
from sqlglot import exp

from services.benchmark.contamination import normalize as contamination_normalize
from services.connectors.tagging import query_context
from services.contracts.protocols import Connector, ContextChunk, LLMProvider, QueryGenerator
from services.contracts.response import (
    CertifiedProvenance,
    ClarificationRequest,
    DataPayload,
    QueryResponse,
    TruncationInfo,
)
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.observability import cost
from services.reviews.judge import judge_trace
from services.reviews.store import Trace as ReviewTrace
from services.runtime import (
    adversary,
    cte_guard,
    faithfulness,
    filter_guard,
    receipt,
    shape_guard,
    sql_guard,
    time_guard,
    value_guard,
    verification,
)
from services.runtime.answer import AnswerComposer, AnswerResult, certified_frame, citations_for
from services.runtime.answer import data_notes as answer_data_notes
from services.runtime.assembly import ANSWER_CONTRACT_SOURCE
from services.runtime.bounded import ServingTimeout, call_bounded
from services.runtime.compiler import CompileError, metric_sql

FETCH_CAP = 5000

# How many rows `logging.log_samples` puts on the request_log row. A sample is a
# debugging aid, not a copy of the result — the trace already carries row_count,
# and a log table that mirrors the warehouse is a second place to leak from.
_SAMPLE_ROWS = 5


def _jsonable(value: object) -> object:
    """Coerce warehouse values (date/datetime/Decimal) to JSON-safe types."""
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


@dataclass
class PipelineResult:
    response: QueryResponse
    trace: TraceLog
    # "warehouse" = the failure happened warehouse-side on governed SQL (dry-run
    # invalid or execution error) — the class the serve-time drift backstop acts
    # on: run the schema diff, file the ticket, template the caller-facing
    # message. None = no failure, or one that is not the warehouse's (provider
    # outage, compose timeout), where a drift check would be noise.
    error_kind: str | None = None


def deterministic_exclusion(question: str, model: SemanticModel) -> str | None:
    """The selection boundary as plain code: the question uses an EXCLUDED
    metric's surface form (word-boundary, underscores read as spaces — the
    clarify matcher's idiom) → the name of that metric, else None. A curator
    who dropped `conversion_rate` from the lens's selection drew a boundary;
    a model asked for it anyway will rebuild it from raw columns and serve it
    — so the refusal must not depend on a model's mood."""
    q = question.lower().replace("_", " ")
    for name in model.excluded_metrics:
        term = name.replace("_", " ").lower().strip()
        if term and re.search(rf"\b{re.escape(term)}\b", q):
            return name
    return None


def _nc_surfaces(entry: dict[str, object]) -> list[str]:
    measure = str(entry.get("measure") or "")
    aliases = entry.get("aliases")
    alias_list = [str(a) for a in aliases] if isinstance(aliases, list) else []
    return [s.replace("_", " ").lower().strip() for s in [measure, *alias_list] if s.strip()]


def _nc_refusal(entry: dict[str, object]) -> str:
    measure = str(entry.get("measure") or "")
    reason = str(entry.get("reason") or "")
    route = str(entry.get("route_to") or "")
    parts = [f"this lens cannot compute {measure}"]
    if reason:
        parts.append(reason)
    parts.append(
        f"ask the '{route}' lens instead"
        if route
        else "no selected entity carries that measure, and approximating it with a "
        "different measure would be a wrong answer wearing a right one's name"
    )
    return " — ".join(parts)


def deterministic_refusal_not_computable(question: str, model: SemanticModel) -> str | None:
    """The authored 'this lens cannot compute X' rail: a question
    naming a declared not-computable measure (or an alias) refuses with the
    reason and the route, deterministically. Word-boundary matches, underscores
    read as spaces — the exclusion rail's idiom. The paraphrase tail this
    cannot see is caught by the ANSWER-echo backstop below: the model narrates
    the canonical measure name even when the question paraphrased it."""
    q = question.lower()
    for entry in model.not_computable:
        for surface in _nc_surfaces(entry):
            if re.search(rf"\b{re.escape(surface)}\b", q):
                return _nc_refusal(entry)
    return None


def not_computable_echo(answer_text: str, model: SemanticModel) -> str | None:
    """The backstop for paraphrases (the audit, the test that matters): 'how
    much is a referral worth over its life' names no declared surface — but
    the model's ANSWER does ('The lifetime value of each referral varies…'),
    because narration translates back to the canonical term. An answer whose
    prose names a not-computable measure on a lens that declared it cannot
    compute it is a substitution, and it refuses instead of serving."""
    a = answer_text.lower()
    for entry in model.not_computable:
        for surface in _nc_surfaces(entry):
            if re.search(rf"\b{re.escape(surface)}\b", a):
                return _nc_refusal(entry)
    return None


def deterministic_clarification(question: str, model: SemanticModel) -> ClarificationRequest | None:
    """The ambiguity trigger as plain code: the question uses an ambiguous
    governed term's surface form (word-boundary, underscores read as spaces)
    and does not explicitly name one of the term's listed meanings (the text
    before each mapping's final " - "). No LLM judgment anywhere in the
    trigger — a governance promise must not depend on a model's mood."""
    q = question.lower()
    for d in model.definitions:
        if d.status != "ambiguous":
            continue
        # The trigger LEXICON is the term plus its authored aliases:
        # identifier-only matching makes the rail unreachable for every
        # realistic phrasing — the declaration reads as governance while
        # behaving as documentation. Still literal word-boundary matches; the
        # aliases widen what users SAY, never what the rail JUDGES.
        surfaces = [d.term.replace("_", " ").lower().strip()] + [
            a.replace("_", " ").lower().strip() for a in d.aliases
        ]
        if not any(s and re.search(rf"\b{re.escape(s)}\b", q) for s in surfaces):
            continue
        meanings = [(m.rpartition(" - ")[0] or m).strip().lower() for m in d.possible_mappings]
        if any(m and m in q for m in meanings):
            continue  # the question picked a meaning explicitly
        return ClarificationRequest(term=d.term, question="", options=list(d.possible_mappings))
    return None


def ambiguity_disclosure(sql: str, model: SemanticModel, question: str = "") -> str | None:
    """The reading a served answer USED, when a declared-ambiguous term's
    mapping is identifiable from the SQL — the deterministic floor beneath the
    clarification rail. Aliases cannot enumerate language: paraphrases carrying
    no alias string reach different populations with no disclosure, all status
    ok. The rail stays the better outcome when it fires; this converts every
    miss from a SILENT wrong-reading answer into a DISCLOSED one.

    Detection is structural, not linguistic: each possible_mapping's tail
    ('meaning - where it lives') names columns/tables, and the served SQL
    either references them or it doesn't. Discloses only when exactly ONE
    mapping matches (an unambiguous attribution) and others exist. The text
    carries no digits — a disclosure must never trip numeric grounding."""
    try:
        tree = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — no disclosure beats a crashed serve
        return None
    if not isinstance(tree, exp.Expression):
        return None
    sql_names = {c.name.lower() for c in tree.find_all(exp.Column)}
    sql_names |= {t.name.lower() for t in tree.find_all(exp.Table)}
    for d in model.definitions:
        if d.status != "ambiguous" or len(d.possible_mappings) < 2:
            continue
        hits: list[str] = []
        misses: list[str] = []
        for m in d.possible_mappings:
            label, sep, tail = m.rpartition(" - ")
            if not sep:
                label, tail = m, ""
            targets = {
                token.split(".")[-1].lower()
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*[A-Za-z0-9_]", tail)
            }
            (hits if targets & sql_names else misses).append(label.strip() or m.strip())
        if len(hits) == 1 and misses:
            if hits[0].lower() in question.lower():
                # The question itself named the reading (the clarify rail's
                # escape hatch) — the caller already knows; annotating would
                # be noise on every explicit ask.
                continue
            others = "; ".join(misses)
            return (
                f"Reading applied: '{d.term}' was read as {hits[0]} — other declared "
                f"readings exist ({others}) and give different numbers; name one to use it."
            )
    return None


def _governed_expressions(model: SemanticModel) -> list[tuple[str, str]]:
    """``(term, expression)`` for everything this lens governs: definitions that
    carry an sql_expr, plus every selected metric's compiled aggregation in both
    the bare and the filter-inlined form — the prompt serializes the first
    (generator.serialize_model) and the filter-repair feedback demands the second
    (filter_guard.corrected_aggregation), so the served SQL may embed either."""
    pairs: list[tuple[str, str]] = []
    for d in model.definitions:
        if d.sql_expr and d.sql_expr.strip():
            pairs.append((d.term, d.sql_expr))
    for entity in model.entities:
        for metric in entity.metrics:
            for inline in (False, True):
                try:
                    pairs.append((metric.name, metric_sql(metric, entity, inline_filters=inline)))
                except CompileError:
                    break  # a malformed metric is validate's problem, not serving's
    return pairs


def _governed_metric_clauses(unfiltered: list[tuple[str, str]]) -> str:
    """The rejection's 'what governs this' sentence, ONE clause per metric.

    ``missing_metric_filters`` reports per FRAGMENT, so a metric governed by two
    filters rendered as two sentences naming the same metric twice — which reads
    as two metrics in conflict, the diagnosis a caller cannot act on (staging
    #40 follow-up). A metric demands all of its filters, so it says so once."""
    by_metric: dict[str, list[str]] = {}
    for name, frag in unfiltered:
        by_metric.setdefault(name, []).append(frag)
    return "; ".join(
        f"this lens's '{name}' metric always applies {' AND '.join(frags)}"
        for name, frags in by_metric.items()
    )


def _actor_label(actor: str) -> str:
    """Prose form of an actor string. The stored form keeps its trust-tier prefix
    (`human:ana@corp` / `token:ci-deploy`) because tiers derive from it; the
    sentence a human reads should name the person or name the token honestly —
    never render `token:x` as if it were a person."""
    if actor.startswith("human:"):
        return actor.removeprefix("human:")
    if actor.startswith("token:"):
        return f"admin token '{actor.removeprefix('token:')}'"
    return actor


def _basis_line(definition_used: str | None, model: SemanticModel) -> str | None:
    """The middle voice: which governed meaning this answer computed, said out loud.

    The failure that outranks every bug: two business users ask for "revenue" in
    their own words and get 17,376,061.96 (`net_revenue`) and 25,864,593.89
    (`total_revenue`) — 49% apart, both `verified`, and NOTHING tells either of
    them which meaning answered. The response knows (`definition_used` is set on
    both); no human-facing surface says it. It is the same shape as the refusal
    asymmetry: the product refuses reliably when nothing is computable, and
    guesses silently when two readings are.

    So: every generated answer that applied a governed meaning names it, with the
    author's own summary, in the field agents already quote verbatim
    (`trust_summary` — the MCP prompt says to lead with it). Two colleagues holding
    different numbers now each hold a line saying WHY, and reconcile in seconds
    instead of in a board meeting.

    Certified provenance overwrites this below — "approved SQL, no AI generation"
    is the stronger claim and carries its own basis. Unattributed answers stay
    None: fabricating a basis would be worse than silence, which is why this
    line depends on attribution being entity-aware (see
    ``_tables_are_present``) — otherwise it can read `total_marketing_spend`
    on a revenue query.
    """
    if not definition_used:
        return None
    for d in model.definitions:
        if d.term == definition_used:
            meaning = (d.summary or d.body.strip().split("\n")[0]).strip().rstrip(".")
            return f"Computed as `{d.term}` — {meaning}. (governed definition)"
    for entity in model.entities:
        for metric in entity.metrics:
            if metric.name == definition_used:
                desc = (metric.description or "").strip().rstrip(".")
                tail = f" — {desc}" if desc else ""
                return f"Computed as `{metric.name}` on `{entity.name}`{tail}. (entity metric)"
    return f"Computed as `{definition_used}`."


def _known_term(term: str | None, model: SemanticModel) -> str | None:
    """*term* when the model actually declares it (definition term or metric
    name — the two namespaces `_basis_line` prints from), else None. A
    self-asserted term the model never declared must not reach basis."""
    if not term:
        return None
    if any(d.term == term for d in model.definitions):
        return term
    if any(m.name == term for e in model.entities for m in e.metrics):
        return term
    return None


def attributed_definition(sql: str, model: SemanticModel) -> str | None:
    """The governed term whose authored expression the SERVED SQL actually contains.

    The generator only ever self-reports *definitions*, so SQL that embedded a
    governed METRIC's expression verbatim reported ``definition_used: null`` and
    dropped out of governed-share reporting (measured: a live answer carrying a
    metric's CASE expression, attributed to nothing). Attribution reads the SQL
    instead of trusting the model's self-report. The longest matching expression
    wins, so a ratio is not attributed to the simple sibling it expands.

    Sibling metrics differing only in `filters` compile to IDENTICAL bare
    expressions and tied on declaration order — the compiler emits the chosen
    metric's name as a SELECT alias, so an alias match outranks length —
    otherwise the SQL is right but the description cited belongs to the sibling."""
    # The verification matcher, deliberately reused rather than forked: it already
    # handles the qualified-vs-bare column case that once failed every commission
    # answer's definition_applied check.
    sql_norm = verification._norm(sql)
    matches = [
        (term, expr)
        for term, expr in _governed_expressions(model)
        if verification._expr_in_sql(expr, sql, sql_norm)
        and _tables_are_present(expr, sql)  # see below
    ]
    if not matches:
        return None
    aliased = [m for m in matches if re.search(rf'\bas\s+"?{re.escape(m[0])}"?\b', sql, re.I)]
    return max(aliased or matches, key=lambda m: len(m[1]))[0]


def _tables_are_present(expr: str, sql: str) -> bool:
    """Every table the expression qualifies must actually appear in the SQL.

    Without this, attribution matched ACROSS ENTITIES on a shared column name.
    ``_expr_in_sql`` falls back to a qualifier-insensitive compare — deliberately,
    so a definition written ``payments.amount`` still matches ``p.amount`` when
    ``p`` IS payments — but stripping the qualifier entirely makes
    ``marketing_spend.amount`` match any SQL summing any column called ``amount``.
    ``amount`` is on payments, refunds and marketing_spend alike.

    Concretely: "net revenue by region" serves correct SQL (captured payments
    minus refunds, grouped by region), touches marketing_spend nowhere, and
    reports ``definition_used: 'total_marketing_spend'`` — which then wins
    outright, because the longest match wins and ``marketing_spend.amount`` is a
    long string.

    Three harms, ascending: the label is wrong; governed-share reporting counts
    column-name collisions as governance; and surfacing ``definition_used`` to the
    user — the fix for two people getting different numbers for "revenue" — would
    turn a silent wrong into a confidently mislabelled one.

    A table the SQL never names cannot have contributed to it, so the check is a
    filter and never a widener: it can only reject matches the old code accepted.
    """
    tables = {
        col.table.lower()
        for col in _parsed_columns(expr)
        if col.table  # bare columns qualify nothing and constrain nothing
    }
    return all(verification._touches(table, sql) for table in tables)


def _parsed_columns(expr: str) -> list[exp.Column]:
    try:
        tree = sqlglot.parse_one(expr)
    except Exception:  # noqa: BLE001 — an unparseable expression constrains nothing
        return []
    return list(tree.find_all(exp.Column)) if tree is not None else []


def run_query(
    *,
    question: str,
    lens_name: str,
    org_id: object,
    caller: str,
    semantic_model: SemanticModel,
    connector: Connector,
    generator: QueryGenerator,
    composer: AnswerComposer | None,
    prose_context: list[ContextChunk] | None = None,
    max_rows: int = 1000,
    compose_rows: int = 200,
    request_id: str | None = None,
    model_name: str = "claude-sonnet-4-6",
    escalate_generator: QueryGenerator | None = None,
    max_repairs: int = 1,
    certification: Literal["certified", "assisted", "none"] = "none",
    certified_match: Literal["exact", "equivalent", "parameterized"] | None = None,
    certified_provenance: CertifiedProvenance | None = None,
    certified_question: str | None = None,
    verified_prose: str | None = None,
    inline_judge_llm: LLMProvider | None = None,
    judge_model: str = "claude-sonnet-4-6",
    adversary_llm: LLMProvider | None = None,
    adversary_model: str = "claude-sonnet-4-6",
    data_as_of: str | None = None,
    serve_ungoverned_shapes: bool = False,
    metric_carriers: Callable[[str], list[str]] | None = None,
    generator_tier: str | None = None,
    log_samples: bool = False,
    value_domains: dict[str, list[str]] | None = None,
) -> PipelineResult:
    """Ground → generate → guard → execute → compose, with execution-guided self-repair.

    The first attempt uses ``generator`` (e.g. a cheap model). If the guard rejects the
    SQL or execution errors, we feed the failure back and retry with ``escalate_generator``
    (a stronger model) if provided, up to ``max_repairs`` times — a cascade that keeps the
    common case cheap while recovering the hard cases.

    ``max_rows`` caps the DATA PAYLOAD the caller gets; ``compose_rows`` is only the
    composer's prompt budget. They are separate because one number for both made a
    271-row deliverable come back as 200 rows to keep the prose cheap. Either cap
    biting is stated out loud — on ``data`` (truncated + row_count), in the
    verification report, and in the answer prose.

    ``FETCH_CAP`` is a third cap, above both: past it the true total is UNKNOWN, so
    ``row_count`` becomes a floor (``row_count_exact=False``) and ``truncated`` is
    true whatever ``max_rows`` says. Measuring the total after the connector applied
    the cap reported the cap itself as the exact count — a 21,056-row answer came
    back claiming 5000, and a lens with ``max_rows_to_return`` at the cap truncated
    in total silence.

    ``composer=None`` serves ROWS ONLY: the compose round-trip — which dominates the
    end-to-end latency of a certified serve — is skipped, and ``answer``
    becomes a deterministic one-liner instead of prose. Everything governance owns
    survives: the guard, the citations, the deterministic verification checks, the
    truncation notes, the trace. Only the prose (and the LLM tiers that grade prose)
    goes. The API maps ``format: "structured"`` to it.

    Certified serves are deterministic end-to-end: a template match
    (``certified_match == "parameterized"``) never composes freely — the answer is
    the code-written frame over the result — and a fixed answer carrying
    ``verified_prose`` (composed once at certify time) serves it VERBATIM. Neither
    makes an LLM call or runs numeric_grounding (the check skips, named); the badge
    is the static certified state. A legacy fixed answer without stored prose
    composes exactly as before.
    """
    rid = request_id or "req_" + uuid.uuid4().hex[:16]
    org = str(org_id)
    prose = prose_context or []
    # Which context actually rode in the prompt. The answer contract is an
    # instruction, not a source the answer drew on — excluded here exactly as the
    # composer excludes it from citations.
    context_refs = [c.source for c in prose if c.source != ANSWER_CONTRACT_SOURCE]
    # Repair attempts consumed — assigned before the trace builders below close
    # over it, since the deterministic refusals trace before the repair loop starts.
    attempt = 0

    ai_in = 0
    ai_out = 0
    ai_cost: float | None = 0.0
    # Real per-stage latency. Generate accumulates across repair attempts.
    started = time.perf_counter()
    latency_ms: dict[str, float] = {}

    def _mark(stage: str, since: float) -> None:
        latency_ms[stage] = round(
            latency_ms.get(stage, 0.0) + (time.perf_counter() - since) * 1000, 1
        )

    def _trace_failure(
        status: str, sql: str | None, reason: str | None, *, error_kind: str | None = None
    ) -> PipelineResult:
        reason = reason or "unknown reason"
        if status == "rejected":
            msg = f"I couldn't form an in-scope query: {reason}"
        elif status == "refused":
            # A governed refusal (the not_computable rail): the reason IS the
            # caller-facing sentence — never dressed as an execution failure.
            msg = reason
        elif error_kind == "generation":
            # The caller message matches the CAUSE: "in-scope" is false for a
            # parse error — the question was fine, and telling
            # the user to rephrase it is how a systematic defect stays
            # invisible (they conclude dst can't answer that shape and stop
            # asking).
            # "Re-asking may succeed" is true for a sampling artifact and false
            # for an invariant the emitter breaks the same way every run: an
            # unbalanced quote reproduces on every attempt, so that advice sends
            # the caller round a loop that cannot terminate. The guard already
            # knows which kind it caught — a deterministic invariant says so.
            retry = (
                "Re-asking will not help — this repeats until it is fixed; report it."
                if reason and "unbalanced backtick" in reason
                else "Re-asking may succeed; if it repeats, report it."
            )
            msg = (
                f"dst generated invalid SQL for this question — a dst defect, not a "
                f"limit of your question or the lens scope ({reason}). {retry}"
            )
        else:
            msg = f"The query could not be executed: {reason}"
        # A certified answer whose SQL errors at execution serves
        # `certified_failed`, never plain `certified` beside `status: error`:
        # a badge that outranks the fault tells the caller the answer is
        # approved while nothing was served.
        served_cert: Literal["certified", "assisted", "none", "certified_failed"] = certification
        if status != "rejected" and certification == "certified":
            served_cert = "certified_failed"
        _mark("total", started)
        out_status = cast(
            'Literal["rejected", "refused", "error"]',
            status if status in ("rejected", "refused") else "error",
        )
        return PipelineResult(
            response=QueryResponse(
                lens=lens_name,
                status=out_status,
                answer=msg,
                # A rejection must not hand the caller the SQL it refused to run
                # ("here's your refusal, and here's the wrong
                # SQL"). The trace keeps it — observability needs the evidence.
                sql=None if status in ("rejected", "refused") else sql,
                certification=served_cert,
                certified_match=certified_match,
                certified_provenance=certified_provenance,
                request_id=rid,
            ),
            error_kind=error_kind,
            trace=TraceLog(
                request_id=rid,
                org_id=org,
                lens=lens_name,
                caller=caller,
                question=question,
                context_refs=context_refs,
                sql=sql,
                valid=False,
                status=out_status,
                error=reason,
                certification=served_cert,
                generator_tier=generator_tier,
                repairs=attempt,
                latency_ms=latency_ms,
                ai_input_tokens=ai_in,
                ai_output_tokens=ai_out,
                ai_cost_usd=round(ai_cost, 6) if ai_cost is not None else None,
            ),
        )

    def _clarification_result(clar: ClarificationRequest) -> PipelineResult:
        canonical = next((d for d in semantic_model.definitions if d.term == clar.term), None)
        if canonical is not None and canonical.possible_mappings:
            clar = clar.model_copy(update={"options": canonical.possible_mappings})
        if not clar.question:
            opts = " or ".join(clar.options) or "one of its meanings"
            clar = clar.model_copy(
                update={"question": f"'{clar.term}' is ambiguous here — do you mean {opts}?"}
            )
        _mark("total", started)
        return PipelineResult(
            response=QueryResponse(
                lens=lens_name,
                status="clarification",
                answer=clar.question,
                sql=None,
                certification=certification,
                clarification=clar,
                request_id=rid,
            ),
            trace=TraceLog(
                request_id=rid,
                org_id=org,
                lens=lens_name,
                caller=caller,
                question=question,
                context_refs=context_refs,
                sql=None,
                status="clarification",
                error=None,
                certification=certification,
                generator_tier=generator_tier,
                repairs=attempt,
                latency_ms=latency_ms,
                ai_input_tokens=ai_in,
                ai_output_tokens=ai_out,
                ai_cost_usd=round(ai_cost, 6) if ai_cost is not None else None,
            ),
        )

    # Selection is a visible boundary, deterministically: a question naming a
    # metric the curator DROPPED from this lens's selection refuses before any
    # generator or warehouse — no LLM gets the chance to reconstruct the
    # excluded computation from raw columns. Same doctrine as the composed-shape
    # guard below, same text (shape_guard.refusal_reason — do not fork the
    # wording), same knob: serve_ungoverned_shapes lets the question proceed to
    # generation and serve at confidence: unverified. Certified answers are
    # exempt (a human approved that exact question→SQL).
    ungoverned_reason: str | None = None
    if certification != "certified":
        dropped = deterministic_exclusion(question, semantic_model)
        if dropped is not None:
            if not serve_ungoverned_shapes:
                return _trace_failure(
                    "rejected",
                    None,
                    shape_guard.refusal_reason([dropped], metric_carriers, named=True),
                )
            # The knob bypassed the boundary — the serve must grade unverified
            # (fold_ungoverned below), or the knob's documented promise is vacuous.
            ungoverned_reason = shape_guard.refusal_reason([dropped], metric_carriers, named=True)

    # The authored cannot-compute boundary: a lens that declared
    # 'no lifetime value here' refuses the question deterministically — the
    # alternative was the model finding the nearest available column and
    # answering confidently about a different measure.
    if certification != "certified":
        nc = deterministic_refusal_not_computable(question, semantic_model)
        if nc is not None:
            return _trace_failure("refused", None, nc)

    # Ask-don't-answer is a GUARANTEE, not a model behavior: the intent path's
    # leaner prompt never renders definitions, so a metric whose name collides
    # with an ambiguous term's surface form ("average value" → avg_clv) would
    # answer without ever seeing the ambiguity, and a prompt rule cannot
    # cover it. Deterministic pre-check, before any
    # generator. Certified answers are exempt by design (a human approved that
    # exact question→SQL).
    if certification != "certified":
        det = deterministic_clarification(question, semantic_model)
        if det is not None:
            return _clarification_result(det)

    feedback: str | None = None
    # The last SQL the guard actually refused, with its reason — kept across repair
    # attempts so a rejection always traces the candidate that caused it. A repair
    # that comes back with no SQL at all (a thinking-starved reply bills tokens with
    # empty content) used to overwrite the real candidate with "", which is how
    # request_log ended up holding status='rejected' rows with a blank sql.
    refused: tuple[str, str | None] | None = None
    refused_kind = "policy"
    # Each candidate SQL's zero-evidence investigation, memoized — a generator
    # that repeats itself (FixedSQLGenerator, or an escalation that lands on the
    # same SQL) cannot loop the probe, and its cached findings still escalate to
    # a clarification when the repair budget runs out.
    probed_empty: dict[str, value_guard.ProbeOutcome] = {}
    # What the empty-result investigation established for the SERVED result:
    # "confirmed" (literals proven present — the zero is the data's answer),
    # "unresolved" (probes taught nothing — the zero serves but never as
    # `verified`), or None (no investigation applied).
    investigation: Literal["confirmed", "unresolved"] | None = None
    while True:
        active = generator if attempt == 0 else (escalate_generator or generator)
        gen_started = time.perf_counter()
        try:
            # Bounded: a wedged provider must surface as an error,
            # not as a request that never returns. The bound is generous —
            # legitimate generations run to 330s — and configurable.
            gen = call_bounded(
                "SQL generation",
                functools.partial(
                    active.generate,
                    question=question,
                    semantic_model=semantic_model,
                    prose_context=prose,
                    dialect=semantic_model.dialect,
                    feedback=feedback,
                ),
            )
        except ServingTimeout as exc:
            _mark("generate", gen_started)
            return _trace_failure("error", refused[0] if refused else None, str(exc))
        _mark("generate", gen_started)
        gen_model = getattr(active, "model", model_name)
        ai_in += gen.input_tokens
        ai_out += gen.output_tokens
        ai_cost = cost.add_cost(
            ai_cost, cost.ai_cost_usd(gen_model, gen.input_tokens, gen.output_tokens)
        )

        if gen.no_answer_reason and not gen.sql.strip():
            # The absence signal: a confident decline with the gap named is a
            # correct answer, not a failure — it does not consume repairs and
            # never reaches the warehouse. A zero from the wrong table would be
            # a wrong answer; this is the alternative the lens promises.
            # status="refused" is also the ONLY decline that is not reproducible:
            # it is the generator's own judgment, so the same question can be
            # answered on the next call. "rejected" and "error" repeat. A caller
            # retrying blindly should retry this one and not those.
            _mark("total", started)
            return PipelineResult(
                response=QueryResponse(
                    lens=lens_name,
                    status="refused",
                    answer=(f"I can't answer this from this lens's data: {gen.no_answer_reason}"),
                    sql=None,
                    certification=certification,
                    request_id=rid,
                ),
                trace=TraceLog(
                    request_id=rid,
                    org_id=org,
                    lens=lens_name,
                    caller=caller,
                    question=question,
                    context_refs=context_refs,
                    # Usually nothing was generated; when an earlier attempt's SQL was
                    # refused before the decline, the trace keeps that candidate.
                    sql=refused[0] if refused else None,
                    status="refused",
                    error=gen.no_answer_reason,
                    certification=certification,
                    generator_tier=generator_tier,
                    repairs=attempt,
                    latency_ms=latency_ms,
                    ai_input_tokens=ai_in,
                    ai_output_tokens=ai_out,
                    ai_cost_usd=round(ai_cost, 6) if ai_cost is not None else None,
                ),
            )

        if gen.clarification is not None and not gen.sql.strip():
            # Ask, don't answer: the question hinges on an AMBIGUOUS governed term.
            # Options come from the model's canonical possible_mappings (the LLM's
            # tailored question wording is kept); never reaches the warehouse and
            # consumes no repair.
            return _clarification_result(gen.clarification)

        # Pre-approved SQL (a certified answer or a certified-definition page's authored query) is
        # trusted on table scope — a human chose those tables, not the model. The
        # generation path stays constrained to the modeled conformed tables.
        guard = sql_guard.check(gen.sql, semantic_model, trust_tables=certification == "certified")
        if not guard.ok or not guard.sql:
            if gen.sql.strip():
                refused = (gen.sql, guard.reason)
                refused_kind = guard.kind
            if attempt < max_repairs:
                # The repair must name what actually failed: for a SYNTAX fault,
                # "rejected by the guard" tells the model it broke a POLICY, so
                # it repairs the wrong dimension — rewriting for scope
                # compliance instead of fixing the quote — and the one repair is
                # spent on a misleading instruction for the most trivially
                # repairable class there is.
                if guard.kind == "syntax":
                    feedback = (
                        f"The previous SQL had a SYNTAX defect: {guard.reason}. "
                        "The question and scope were fine — fix only the syntax "
                        f"and re-emit. Previous SQL: {gen.sql}"
                    )
                else:
                    feedback = (
                        f"The previous SQL was rejected by the guard: {guard.reason}. "
                        f"Previous SQL: {gen.sql}"
                    )
                attempt += 1
                continue
            # The refused SQL and the reason it was refused always describe the same
            # attempt; with nothing generated on any attempt there is no SQL to keep
            # and the guard's own reason (it names an empty generation) stands.
            # A syntax fault is dst's OWN defect, and it lands as an ERROR — a
            # governance decline it is not. Booking dst faults as governed
            # outcomes inverts the health metric and sends callers off to
            # rephrase questions that were fine. The kind follows the REPORTED
            # failure: an empty repair after a policy refusal must not
            # reclassify the refusal.
            reported_kind = refused_kind if refused else guard.kind
            if reported_kind == "syntax":
                return _trace_failure(
                    "error",
                    *(refused or (None, guard.reason)),
                    error_kind="generation",
                )
            return _trace_failure("rejected", *(refused or (None, guard.reason)))

        # Binder guarantee (repair-only, generation path): a CTE referenced for a
        # column it never projected is definitionally invalid SQL. The dry-run gate
        # below already REJECTS that SQL — this runs earlier only to spend the one
        # repair attempt on a sharper instruction than the engine's raw binder error
        # (too vague to land a repair), and to cover connectors
        # with no dry run. Repair-only by contract: when attempts are exhausted the
        # SQL flows on to the same gates as before — this guard can spend a repair,
        # never block, so a false positive costs feedback quality and nothing else.
        if certification != "certified":
            unprojected = cte_guard.underprojected_cte_refs(guard.sql, semantic_model.dialect)
            if unprojected and attempt < max_repairs:
                feedback = cte_guard.repair_feedback(unprojected, guard.sql)
                attempt += 1
                continue

        # Filter guarantee (guard-class, generation path only): a metric's filters are
        # part of its meaning, and the raw-SQL path's "REQUIRES filters" prompt line is
        # advisory — a serving model can ignore it. Deterministic check in code;
        # certified SQL is exempt like table trust (a human approved it).
        if certification != "certified":
            unfiltered = filter_guard.missing_metric_filters(
                guard.sql, semantic_model, question=question
            )
            if unfiltered:
                demands = "; ".join(f"metric '{name}' REQUIRES {frag}" for name, frag in unfiltered)
                if attempt < max_repairs:
                    # Copy-pasteable feedback: the FULL governed aggregation
                    # (filters inlined), not just the bare condition — given
                    # only the condition, the model re-emits its own
                    # unfiltered form and the next attempt hard-refuses.
                    fixes = "; ".join(
                        f"compute '{name}' exactly as {agg}"
                        for name in dict.fromkeys(n for n, _f in unfiltered)
                        if (agg := filter_guard.corrected_aggregation(name, semantic_model))
                    )
                    fix_line = f" Emit the governed aggregation verbatim: {fixes}." if fixes else ""
                    feedback = (
                        f"The previous SQL computed a governed metric WITHOUT its required "
                        f"filter(s): {demands}.{fix_line} Alternatively AND each condition "
                        f"into the WHERE clause. Previous SQL: {guard.sql}"
                    )
                    attempt += 1
                    continue
                # A silently unfiltered governed metric is a WRONG answer, not a
                # degraded one — reject rather than serve it, telling the caller
                # what governs the metric instead of naming guard internals.
                governs = _governed_metric_clauses(unfiltered)
                # Name the actual conflict when there is one: "try rephrasing"
                # cannot help when two governed twins demand incompatible
                # filters — no phrasing satisfies both, so that advice hides
                # the deadlock instead of surfacing it.
                note = filter_guard.conflict_note(unfiltered, semantic_model)
                tail = (
                    note
                    if note
                    else "include the filter shown, or ask for the governed metric by name"
                )
                return _trace_failure(
                    "rejected",
                    guard.sql,
                    f"governed metric filter missing: {governs} — the answer could not "
                    f"be computed with the required filter(s) applied; {tail}",
                )

        # Ratio-population guarantee: a ratio whose denominator
        # filters on the numerator's own aggregated column mixes populations —
        # a figure that measures nothing. The model has no reason to write it
        # deliberately, so one repair with the predicate named usually lands;
        # if it survives, the ratio_population check fails on the report and
        # caps confidence (never a rejection — the narrow rule is FP-safe but
        # intent can be unusual, and a capped serve beats a false refusal).
        if certification != "certified" and attempt < max_repairs:
            ratio_mismatch = verification.ratio_population_mismatch(
                guard.sql, semantic_model.dialect
            )
            if ratio_mismatch is not None:
                feedback = (
                    f"The previous SQL mixed populations in a ratio: {ratio_mismatch}. "
                    f"Previous SQL: {guard.sql}"
                )
                attempt += 1
                continue

        # Time-grain guarantee (guard-class, generation path only): GROUPing BY a
        # raw TIMESTAMP column is a per-event grain wearing a per-period costume —
        # "sessions per month" grouped on a raw timestamp serves ~5000 one-row
        # groups, and the model agrees the SQL was wrong when asked. Deterministic
        # post-check beside filter_guard; DATE-typed columns raw-grouped are legal
        # daily grain and stay untouched. Certified SQL is exempt like table trust.
        if certification != "certified":
            raw_groups = time_guard.raw_timestamp_groupings(guard.sql, semantic_model)
            if raw_groups:
                period = time_guard.asked_period(question)
                cols = ", ".join(f"'{entity}.{col}'" for entity, col in raw_groups)
                fix = "; ".join(f"DATE_TRUNC('{period}', {col})" for _entity, col in raw_groups)
                if attempt < max_repairs:
                    feedback = (
                        f"The previous SQL GROUPed BY the raw timestamp column(s) {cols} — "
                        f"group by a bucketed period — {fix} — never the raw timestamp. "
                        f"Previous SQL: {guard.sql}"
                    )
                    attempt += 1
                    continue
                # A five-thousand-group "monthly" answer is a WRONG answer —
                # reject naming the fix rather than serve it.
                return _trace_failure(
                    "rejected",
                    guard.sql,
                    f"time grouping not bucketed: group by {fix}, never the raw "
                    f"timestamp column(s) {cols}",
                )

        # Value guarantee (guard-class, generation path only): a filter literal
        # outside a column's COMPLETE sampled value dictionary can only match
        # rows by accident — WHERE country = 'Finland' against a column holding
        # 'FI' is the confident-zero shape. Feedback names the real values;
        # repairs exhausted with the literal still outside the domain, the answer
        # is a deterministic unknown_value clarification, never the accidental
        # zero: the domain is COMPLETE, so executing could only dress the mismatch
        # up as data (ask-don't-guess, extended from terms to values).
        if certification != "certified" and value_domains:
            misses = value_guard.unknown_literals(guard.sql, semantic_model.dialect, value_domains)
            if misses:
                if attempt < max_repairs:
                    feedback = value_guard.repair_feedback(misses, guard.sql)
                    attempt += 1
                    continue
                miss = misses[0]
                return _clarification_result(
                    value_guard.unknown_value_clarification(
                        miss.column, (miss.literal,), miss.values
                    )
                )

        # Ungoverned-shape guarantee (generation path only): SQL
        # that COMPOSES a dropped shared-layer metric from raw columns — a
        # conversion rate as a GROUP BY'd flag count, divided in prose — refuses with
        # the path instead of serving at low confidence. No repair attempt: the fix
        # is a curator's (select the metric in lens.yaml, or certify), not a model's;
        # the serve_ungoverned_shapes lens knob opts back into serving it unverified.
        if certification != "certified":
            composed = shape_guard.composed_excluded_shapes(guard.sql, semantic_model)
            if composed:
                if not serve_ungoverned_shapes:
                    return _trace_failure(
                        "rejected",
                        guard.sql,
                        shape_guard.refusal_reason(composed, metric_carriers),
                    )
                ungoverned_reason = shape_guard.refusal_reason(composed, metric_carriers)

        # Dry-run gate: validate — and on engines that estimate scan
        # cost, budget-check — the SQL before any job runs. An invalid or
        # over-budget query is discovered as cheap pre-flight feedback for the
        # repair loop, not by running it. Best-effort: a broken or missing
        # dry_run never blocks a valid query (test doubles may omit it).
        dryrun_started = time.perf_counter()
        pre = None
        if (dry := getattr(connector, "dry_run", None)) is not None:
            try:
                pre = dry(guard.sql)
            except Exception:
                pre = None
        _mark("dryrun", dryrun_started)
        if pre is not None:
            cap = getattr(connector, "max_bytes", None)
            over_budget = pre.valid and cap and pre.bytes_estimated and pre.bytes_estimated > cap
            if not pre.valid or over_budget:
                reason = (
                    f"the query would scan {pre.bytes_estimated:,} bytes, over this "
                    f"lens's {cap:,}-byte budget — narrow it (tighter WHERE, a "
                    "partition/date filter, fewer columns)"
                    if over_budget
                    else (pre.error or "the SQL failed the warehouse's dry-run validation")
                )
                if attempt < max_repairs:
                    feedback = (
                        f"The previous SQL failed pre-flight validation without being "
                        f"run: {reason}. Previous SQL: {guard.sql}"
                    )
                    attempt += 1
                    continue
                return _trace_failure("error", guard.sql, reason, error_kind="warehouse")

        exec_started = time.perf_counter()
        try:
            # FETCH_CAP + 1: one row past the cap is how we learn the cap BIT.
            # Fetching exactly FETCH_CAP makes "the query returned 5000" and "the
            # query returned 21,056 and we cut it" the same observation, and the
            # pipeline then reports the cap as if it were the true count.
            with query_context(purpose="serve", lens=lens_name):
                result = connector.execute(guard.sql, read_only=True, row_limit=FETCH_CAP + 1)
        except Exception as exc:  # noqa: BLE001 — surface as a graceful error + repair attempt
            _mark("execute", exec_started)
            if attempt < max_repairs:
                feedback = f"The previous SQL failed to execute: {exc}. Previous SQL: {guard.sql}"
                attempt += 1
                continue
            return _trace_failure("error", guard.sql, str(exc), error_kind="warehouse")
        _mark("execute", exec_started)

        # Probe-on-empty (generation path only): zero EVIDENCE behind a
        # string-literal filter is the confident-zero shape — zero rows, or the
        # one-row aggregate costume (COUNT → 0, other aggregates → NULL) that
        # `has_rows` cannot see. The filter may be written in the question's
        # vocabulary instead of the column's, and no structural guard can see
        # that. One governed investigation per candidate SQL (≤2 SELECT DISTINCT
        # reads, capped): a literal proven absent becomes repair feedback while
        # repairs remain, and an unknown_value clarification when they are
        # exhausted — never the accidental zero. A present literal is an honest
        # zero and serves; a probe that taught nothing serves too, graded below
        # `verified` by the empty_result_investigation check.
        if certification != "certified" and (
            not result.rows or value_guard.zero_evidence(guard.sql, semantic_model.dialect, result)
        ):
            outcome = probed_empty.get(guard.sql)
            if outcome is None:
                probe_started = time.perf_counter()
                outcome = value_guard.empty_result_probe(guard.sql, semantic_model, connector)
                _mark("probe", probe_started)
                probed_empty[guard.sql] = outcome
            if outcome.findings:
                if attempt < max_repairs:
                    feedback = value_guard.probe_feedback(outcome.findings, guard.sql)
                    attempt += 1
                    continue
                finding = outcome.findings[0]
                return _clarification_result(
                    value_guard.unknown_value_clarification(
                        finding.column, finding.missing, finding.values
                    )
                )
            if outcome.attempted:
                investigation = "confirmed" if outcome.confirmed else "unresolved"
        break

    # Attribution reads the SERVED SQL, and only falls back to it: the generator's
    # self-report names definitions it chose to mention and never names a metric, so
    # SQL built from a governed metric's authored expression reported nothing and
    # undercounted the governed share. A self-report naming NOTHING in the model
    # is dropped first — otherwise basis prints a fabricated `Computed as X` for
    # terms the model never declared.
    definition_used = _known_term(gen.definition_used, semantic_model) or attributed_definition(
        guard.sql, semantic_model
    )

    # The engine-side cap bit: the query matched MORE than we fetched, so the true
    # count is unknown and everything downstream must say "more than", never a
    # number it cannot stand behind. Trim the probe row back off so no consumer
    # (composer, verification, payload) ever sees FETCH_CAP + 1 rows.
    fetch_capped = len(result.rows) > FETCH_CAP
    served = result.model_copy(update={"rows": result.rows[:FETCH_CAP]}) if fetch_capped else result
    total_rows = len(served.rows)
    # The PAYLOAD is short — data the caller never sees. Either cap can do it, and
    # the fetch cap counts even when max_rows would have returned everything we
    # HELD: rows the query matched and the caller never received are truncation.
    truncated = fetch_capped or total_rows > max_rows

    # On a certified serve, no model writes the answer. ``static_prose``
    # names why numeric_grounding SKIPS (there is no free prose to catch
    # inventing); the badge is the static certified state, and the text is
    # byte-identical across serves of the same result.
    static_prose: str | None = None
    if composer is None:
        # Rows-only serve: no compose round-trip at all. `answer` must still not come
        # back EMPTY — every programmatic caller in and around this repo grades
        # `data.rows` and reads `answer` only when something went wrong, so a blank one
        # reads as a failure. A deterministic line naming the shape of the result, and
        # why there is no prose, is the honest stand-in; the citations are governance
        # and survive unchanged. Past the fetch cap the count is a floor, so the line
        # never states a total it cannot stand behind.
        count = f"more than {total_rows}" if fetch_capped else str(total_rows)
        noun = "row" if total_rows == 1 and not fetch_capped else "rows"
        ans = AnswerResult(
            text=(
                f"Structured answer: {count} result {noun} in `data`; "
                "no prose was composed (format=structured)."
            ),
            citations=citations_for(gen, prose),
        )
    elif certification == "certified" and certified_match == "parameterized":
        # A certified TEMPLATE never composes freely: one sample's prose cannot
        # speak for every binding, so the answer is the deterministic frame —
        # question restated over the result, money at 2dp from the model's own
        # declarations, written entirely by code (no LLM call on the path).
        ans = AnswerResult(
            text=certified_frame(
                question,
                served,
                semantic_model,
                max_rows=compose_rows,
                row_count_exact=not fetch_capped,
            ),
            citations=citations_for(gen, prose),
        )
        static_prose = (
            "deterministic certified render — the answer text was written by code "
            "from the result rows, no prose was composed"
        )
    elif certification == "certified" and verified_prose is not None:
        # A fixed certified answer serves its certify-time prose VERBATIM: the
        # English was composed once from the executed result when a human
        # certified it, and re-composing per request is where the ~6s and the
        # badge wobble lived. No composer call, no reconcile —
        # the stored prose already went through both at certify time.
        ans = AnswerResult(text=verified_prose, citations=citations_for(gen, prose))
        static_prose = (
            "certified prose served verbatim — composed and grounded once at "
            "certify time, no prose was composed for this request"
        )
    elif investigation == "unresolved":
        # A detected-unsafe zero never reaches the composer: left to a model,
        # the same unresolved-empty event coin-flips between "zero customers
        # are using AI replies" asserted as fact and an honest decline —
        # whichever the model happens to write that run. One code path,
        # deterministic text; the empty_result_investigation check still FAILS
        # on the report, so the grade stays below verified.
        ans = AnswerResult(
            text=value_guard.unconfirmed_zero_text(guard.sql, semantic_model.dialect),
            citations=citations_for(gen, prose),
        )
        static_prose = (
            "deterministic unconfirmed-zero render — the empty result could not be "
            "confirmed, so no prose was composed and the zero is not asserted"
        )
    else:
        compose_started = time.perf_counter()
        try:
            ans = call_bounded(
                "answer composition",
                functools.partial(
                    composer.compose,
                    question=question,
                    generated=gen,
                    result=served,
                    semantic_model=semantic_model,
                    prose_context=prose,
                    max_rows=compose_rows,
                    row_count_exact=not fetch_capped,
                ),
            )
        except ServingTimeout as exc:
            _mark("compose", compose_started)
            return _trace_failure("error", guard.sql, str(exc))
        _mark("compose", compose_started)
        # The prose and the rows must not be two answers. The composer is a model, so
        # it can always round a figure it was told to quote — this deterministic pass
        # rewrites any such rounding back to the row's own value, making the prose a
        # rendering of `data` rather than a second computation of it. Rounding drift
        # is the dominant way one value ends up published two ways.
        ans.text = faithfulness.reconcile(
            ans.text,
            served,
            definition_text=next(
                (d.body for d in semantic_model.definitions if d.term == definition_used), ""
            ),
            question_text=question,
            notes_text=answer_data_notes(semantic_model, served.columns),
            sql_text=guard.sql,
            dialect=semantic_model.dialect,
            clock_years=verification.clock_years(semantic_model.timezone),
        )
        compose_model = getattr(composer, "model", model_name)
        ai_in += ans.input_tokens
        ai_out += ans.output_tokens
        ai_cost = cost.add_cost(
            ai_cost, cost.ai_cost_usd(compose_model, ans.input_tokens, ans.output_tokens)
        )

    # The answer-echo backstop for not-computable measures: the
    # question-side rail cannot enumerate paraphrases, but the model's OWN
    # NARRATION translates them back to the canonical term ('The lifetime
    # value of each referral varies…') — a deterministic hook no phrase list
    # gives the question side. An answer whose prose names a measure this lens
    # declared it cannot compute is a substitution: refuse, never serve.
    if certification != "certified":
        nc_echo = not_computable_echo(ans.text, semantic_model)
        if nc_echo is not None:
            return _trace_failure("refused", guard.sql, nc_echo)

    # The ambiguity-disclosure floor: when the clarification
    # rail missed (aliases cannot enumerate language) and the SQL identifiably
    # used ONE declared reading, the answer says which — every miss becomes a
    # disclosed reading instead of a silent one. Certified serves are exempt:
    # a human approved that SQL's meaning.
    disclosure = (
        ambiguity_disclosure(guard.sql, semantic_model, question)
        if certification != "certified"
        else None
    )
    if disclosure:
        ans.text = f"{ans.text.rstrip()} {disclosure}" if ans.text else disclosure

    report = verification.build_report(
        question=question,
        sql=guard.sql,
        answer_text=ans.text,
        result=served,
        semantic_model=semantic_model,
        definition_used=definition_used,
        truncated=truncated,
        row_count_exact=not fetch_capped,
        certification=certification,
        composed=composer is not None and static_prose is None,
        uncomposed_reason=static_prose,
        investigation=investigation,
        data_as_of=data_as_of,
    )
    # Figure gate: a composed answer whose numeric_grounding check FAILS
    # is an invented figure on its way to a caller. Retry composition ONCE with
    # the failure named in-prompt; still failing, the prose is WITHHELD and the
    # data block serves behind a deterministic frame — a degraded-but-true answer
    # is an outcome, an invented figure is not. Generated path only:
    # certified prose is handled on the certified path (a numeric fail there demotes the
    # grade), and a rows-only serve composed nothing to gate.
    composition: Literal["fallback"] | None = None
    withheld_draft: str | None = None
    if composer is not None and certification != "certified":
        gate = report.check("numeric_grounding")
        if gate is not None and gate.status == "fail":
            retry_started = time.perf_counter()
            retry: AnswerResult | None
            try:
                retry = call_bounded(
                    "answer composition (figure-gate retry)",
                    functools.partial(
                        composer.compose,
                        question=question,
                        generated=gen,
                        result=served,
                        semantic_model=semantic_model,
                        prose_context=prose,
                        max_rows=compose_rows,
                        row_count_exact=not fetch_capped,
                        feedback=gate.reason,
                    ),
                )
            except ServingTimeout:
                # The first compose already yielded an answer shape — a wedged
                # retry falls through to the deterministic frame rather than
                # turning a servable request into an error.
                retry = None
            _mark("compose", retry_started)
            if retry is not None:
                retry_model = getattr(composer, "model", model_name)
                ai_in += retry.input_tokens
                ai_out += retry.output_tokens
                ai_cost = cost.add_cost(
                    ai_cost, cost.ai_cost_usd(retry_model, retry.input_tokens, retry.output_tokens)
                )
                retry.text = faithfulness.reconcile(
                    retry.text,
                    served,
                    definition_text=next(
                        (d.body for d in semantic_model.definitions if d.term == definition_used),
                        "",
                    ),
                    question_text=question,
                    notes_text=answer_data_notes(semantic_model, served.columns),
                    sql_text=guard.sql,
                    dialect=semantic_model.dialect,
                    clock_years=verification.clock_years(semantic_model.timezone),
                )
                regraded = verification.build_report(
                    question=question,
                    sql=guard.sql,
                    answer_text=retry.text,
                    result=served,
                    semantic_model=semantic_model,
                    definition_used=definition_used,
                    truncated=truncated,
                    row_count_exact=not fetch_capped,
                    certification=certification,
                    composed=True,
                    investigation=investigation,
                )
                regate = regraded.check("numeric_grounding")
                if regate is None or regate.status != "fail":
                    ans, report = retry, regraded
            final = report.check("numeric_grounding")
            if final is not None and final.status == "fail":
                # Twice-failed prose never ships. The FAILING report is kept on
                # purpose: it is the evidence for the fallback, it grades the
                # serve unverified, and that lands it in auto_review's net —
                # regrading the deterministic frame would launder the incident.
                count = f"more than {total_rows}" if fetch_capped else str(total_rows)
                noun = "row" if total_rows == 1 and not fetch_capped else "rows"
                # The rows themselves ride the fallback: the correct values are
                # sitting right there in the payload, and without them the
                # caller reads a row-count shrug as failure. Withholding stays
                # about the PROSE — the deterministic table is a rendering of the
                # data, exactly what a withheld composition degrades to.
                from services.runtime.answer import _format_rows

                preview_rows = served.rows[:10]
                preview = _format_rows(served.columns, preview_rows)
                more = (
                    f"\n… and {total_rows - len(preview_rows)} more row(s) in `data`."
                    if total_rows > len(preview_rows)
                    else ""
                )
                # The rejected draft rides the TRACE: without it, an operator can
                # see that something was ungrounded but never WHAT, and
                # diagnosing a grounding false positive means reconstructing the
                # candidate prose offline. Same doctrine as the refused SQL:
                # withheld from the caller, kept
                # as evidence. Captured before the frame overwrites it.
                withheld_draft = ans.text
                # The fallback NAMES the rejected claim:
                # the check's reason quotes it as written, and without it the
                # only way to diagnose a withheld answer was to guess at the
                # composer's wording — the class stops being self-diagnosing.
                why = f" ({final.reason})" if final.reason else ""
                ans.text = (
                    f"{question} — {count} result {noun} × {len(served.columns)} "
                    "columns in `data`. No prose: the composed answer stated "
                    f"figures not grounded in the result, and was withheld{why}. "
                    f"The result itself:\n{preview}{more}"
                )
                composition = "fallback"
    # Opt-in inline judge tier — high-stakes lenses pay one extra round-trip
    # for a rubric audit of the whole chain; certified answers never need it.
    # Both extra tiers grade a chain that ENDS IN PROSE ("does the answer follow from
    # the result, with no invented numbers?"), so a rows-only serve has nothing for
    # them to rule on — they don't run, exactly as on a lens that never enabled them,
    # and the report carries no judge/adversary check to claim otherwise. A caller who
    # wants prose-inclusive review asks with format=both. A fallback serve has no
    # prose left either — its frame is code, and the failing report already says
    # everything a reviewer needs.
    # The certified carve-out is scoped to VERBATIM serves: human-approved SQL
    # should not be second-guessed *for the question it was approved for* — but
    # fuzzy matching serves it for questions nobody approved, and that is
    # exactly when a second opinion matters most. Exempting every certified
    # serve means the judge never runs on the fuzzy ones, so it can demote
    # nothing there. A serve whose asked question is not the certified
    # question (normalized) gets the same judge/adversary a generated serve
    # gets; an unknown certified question (None) counts as fuzzy — fail safe.
    certified_verbatim = certification == "certified" and (
        certified_question is not None
        and contamination_normalize(certified_question) == contamination_normalize(question)
    )
    if (
        inline_judge_llm is not None
        and not certified_verbatim
        and composer is not None
        and composition is None
    ):
        judge_started = time.perf_counter()
        verdict, reasoning = judge_trace(
            inline_judge_llm,
            ReviewTrace(
                request_id=rid,
                lens=lens_name,
                caller=caller,
                question=question,
                sql=guard.sql,
                answer=ans.text,
                definition_used=definition_used,
                confidence=report.grade,
                row_count=total_rows,
            ),
            judge_model,
        )
        _mark("judge", judge_started)
        report = verification.fold_judge(report, verdict, reasoning)
    # Opt-in adversarial reviewer — a second opinion that challenges the
    # answer's assumptions (definition choice, exclusions, grain, time window)
    # rather than scoring a rubric. VERBATIM certified serves skip it: the SQL
    # is human-approved for this exact question, so there is no assumption left
    # to challenge. A fuzzy certified serve keeps it — "this answer covers the
    # asked question" is itself the challengeable assumption.
    if (
        adversary_llm is not None
        and not certified_verbatim
        and composer is not None
        and composition is None
    ):
        adversary_started = time.perf_counter()
        challenges = adversary.challenge(
            adversary_llm,
            question=question,
            sql=guard.sql,
            answer=ans.text,
            semantic_model=semantic_model,
            model=adversary_model,
        )
        _mark("adversary", adversary_started)
        report = verification.fold_challenges(report, challenges)
    # Folded LAST so nothing upgrades past it: a serve that exists only because
    # serve_ungoverned_shapes bypassed the selection boundary grades unverified.
    if ungoverned_reason is not None:
        report = verification.fold_ungoverned(report, ungoverned_reason)
    confidence = report.grade
    _mark("total", started)

    # A truncated answer says so in the prose, deterministically — the composer's
    # "truncated" prompt hint is advisory, and without the note a consumer's only
    # tell is a suspiciously round row count. Appended AFTER build_report: these
    # counts are the pipeline's, and numeric_grounding must not read them as
    # numbers the model invented.
    # Past the fetch cap the total is UNKNOWN, so the note says "more than" and
    # never a precise-looking number — reporting the cap as the count is what made
    # a 21,056-row answer read as a 5000-row one. Raising max_rows_to_return is
    # also the wrong advice there (the fetch cap is above it), so the two cases
    # give different instructions.
    returned = min(max_rows, total_rows)
    answer_text = ans.text
    if fetch_capped:
        answer_text += (
            f" Note: this answer returns {returned} rows, but the query matched more "
            f"than {total_rows} — more than dst fetches in one request — so the "
            "full total is not known here; totals beyond this window are not "
            "stated. Aggregate in SQL, or page with LIMIT/OFFSET, to cover the "
            "whole result."
        )
    elif truncated:
        answer_text += (
            f" Note: this answer returns only the first {returned} of {total_rows} "
            "result rows — totals beyond this window are not stated; narrow the "
            "question, or raise the lens's max_rows_to_return, to get the rest."
        )
    elif composer is not None and total_rows > compose_rows:
        answer_text += (
            f" Note: the summary above reads the first {compose_rows} of {total_rows} "
            "result rows; the full result is in the data payload."
        )

    # A departure from a governed definition is said OUT LOUD, in the same
    # deterministic-append idiom as the truncation notes above and for the same
    # reason: a composer hint is advisory, this is not. Attribution, not
    # prohibition — the answer is still served, because a lens that refuses a
    # caller who asked knowingly is a lens that gets routed around. What the
    # caller may not do is depart SILENTLY: without this, an in-band override
    # ("ignore the label, the PI signed off") serves "343 molecules are
    # carcinogenic" at confidence `verified` with every check green.
    departure = next(
        (c for c in report.checks if c.name == "definition_applied" and c.status == "fail"),
        None,
    )
    if departure is not None and departure.reason:
        answer_text += f" Note: {departure.reason} — this answer is not governed by it."

    # The freshness contract's violation is said in-band, same deterministic-append
    # idiom: the caller reading only the prose must not hold month-old data as
    # current when the lens itself declares it stale.
    stale = next(
        (c for c in report.checks if c.name == "freshness" and c.status == "fail"),
        None,
    )
    if stale is not None and stale.reason:
        answer_text += f" Note: {stale.reason}."

    # The data's age is stated UNPROMPTED: marts routinely run a day or two
    # behind, and a whole class of wrong answers is "current" resolved against
    # a lagging snapshot — the date makes those self-evident. Same
    # deterministic-append idiom; skipped when the prose
    # already carries the date, and on windowless envelopes (no text).
    if data_as_of and answer_text and (as_of_day := str(data_as_of)[:10]) not in answer_text:
        answer_text += f" (data as of {as_of_day})"

    data = DataPayload(
        columns=served.columns,
        rows=[[_jsonable(v) for v in row] for row in served.rows[:max_rows]],
        row_count=total_rows,
        row_count_exact=not fetch_capped,
        truncated=truncated,
    )
    trust_summary = _basis_line(definition_used, semantic_model)
    # The scope travels with the number: an answer computed over
    # a scoped subset names the population in the field agents lead with —
    # whether or not the filter was enforced (the check grades that half).
    population_scopes = [
        e.population
        for e in semantic_model.entities
        if e.population
        and re.search(
            rf"\b{re.escape(e.name)}\b|\b{re.escape(e.source.table.split('.')[-1])}\b",
            guard.sql,
            re.IGNORECASE,
        )
    ]
    if certified_provenance is not None:
        approver = _actor_label(certified_provenance.certified_by)
        if certified_match == "parameterized" and certified_provenance.bound_values:
            # "approved SQL, your values" is a different trust claim
            # than "approved SQL verbatim" — say the values out loud.
            values = ", ".join(
                f"{k}={v}" for k, v in sorted(certified_provenance.bound_values.items())
            )
            trust_summary = (
                f"Certified template — approved by {approver} "
                f"on {certified_provenance.certified_at[:10]}; approved SQL shape, "
                f"your values: {values}. No AI SQL generation."
            )
        else:
            trust_summary = (
                f"Certified answer — approved by {approver} "
                f"on {certified_provenance.certified_at[:10]}; "
                "served from approved SQL, no AI generation."
            )
    if population_scopes:
        scope_line = "Population: " + "; ".join(dict.fromkeys(population_scopes)) + "."
        trust_summary = f"{trust_summary} {scope_line}" if trust_summary else scope_line
    if disclosure:
        # The reading rides the field agents lead with, not only the prose —
        # a consumer that never renders the answer text still sees which
        # meaning the number carries.
        trust_summary = f"{trust_summary} {disclosure}" if trust_summary else disclosure
    # Abstentions are announced, not just recorded: the full check ledger
    # already rides `verification`, but `degraded` would stay empty exactly
    # when the answer was graded on partial evidence, leaving no
    # human-readable surface to say that a `verified` rested on 3 of 6
    # checks. Certified serves are exempt: their
    # guarantee is the approval, and their checks skip structurally.
    degraded_notes: list[str] = []
    if certification != "certified" and report is not None:
        # The trigger is THE SAME line the grade draws
        # (`_semantic_abstained`): announce exactly when the badge was capped for
        # abstention. A structural skip (freshness not declared) on every
        # healthy serve would be the noise nobody reads — the pinned
        # other-half of loud.
        skips = [c for c in report.checks if c.status == "skip"]
        if skips and verification._semantic_abstained(report.checks):
            # Reasons name only the abstaining SEMANTIC checks — a structural
            # skip's reason ("the question states no time window") is not part
            # of the abstention story and would bury it.
            reasons = "; ".join(
                f"{c.name}: {c.reason}"
                for c in skips
                if c.reason and c.name in ("definition_applied", "intent_alignment")
            )
            caveat = f"graded on {len(report.checks) - len(skips)} of {len(report.checks)} checks"
            degraded_notes.append(f"{caveat}{f' — {reasons}' if reasons else ''}")
            trust_caveat = caveat[0].upper() + caveat[1:]
            trust_summary = f"{trust_summary} {trust_caveat}." if trust_summary else trust_caveat
    return PipelineResult(
        response=QueryResponse(
            lens=lens_name,
            answer=answer_text,
            data=data,
            sql=guard.sql,
            definition_used=definition_used,
            citations=ans.citations,
            confidence=confidence,
            verification=report,
            degraded=degraded_notes,
            certification=certification,
            certified_match=certified_match,
            certified_provenance=certified_provenance,
            trust_summary=trust_summary,
            data_as_of=data_as_of,
            receipt=receipt.build(
                request_id=rid,
                lens=lens_name,
                certification=certification,
                cert_id=certified_provenance.cert_id if certified_provenance else None,
                confidence=confidence,
                sql=guard.sql,
                data_as_of=data_as_of,
            ),
            composition=composition,
            # The stamped fact, on the envelope: `data` is dropped for
            # format=prose, and a caller must never have to parse English to
            # learn the window was short. Past the fetch cap the true total is
            # unknown — total: None, never a number the pipeline can't stand
            # behind.
            truncated=(
                TruncationInfo(returned=returned, total=None if fetch_capped else total_rows)
                if truncated
                else None
            ),
            request_id=rid,
        ),
        trace=TraceLog(
            request_id=rid,
            org_id=org,
            lens=lens_name,
            caller=caller,
            question=question,
            context_refs=context_refs,
            sql=guard.sql,
            valid=True,
            row_count=total_rows,
            # `logging.log_samples` was a switch with no writer and `sample` a
            # column with no author — declared observability that never observed.
            # It stays off by default: the sample is real result values, and the
            # log row outlives the request.
            sample=[list(row) for row in served.rows[:_SAMPLE_ROWS]] if log_samples else None,
            answer=(
                f"{answer_text}\n[withheld draft: {withheld_draft}]"
                if withheld_draft
                else answer_text
            ),
            citations=ans.citations,
            definition_used=definition_used,
            confidence=confidence,
            verification=report,
            certification=certification,
            generator_tier=generator_tier,
            repairs=attempt,
            latency_ms=latency_ms,
            status="ok",
            ai_input_tokens=ai_in,
            ai_output_tokens=ai_out,
            ai_cost_usd=round(ai_cost, 6) if ai_cost is not None else None,
            wh_bytes=result.bytes_scanned,
            wh_cost_usd=cost.warehouse_cost_usd(result.bytes_scanned, connector.kind),
        ),
    )
