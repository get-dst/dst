"""Answer composer — turns query results into a cited, grounded answer.

Caps the rows fed to the LLM (token/cost), states the applied definition, and
computes a confidence signal from concrete facts (not LLM self-report).

The composer is the SECOND model call of a served request, and the easy mistake
is leaving it domain-blind: hand it `semantic_model` and use it for nothing, and
the model that writes the English has no way to know what a stored code means.
Take a lens where `bond_type = '-'` is the single bond and the lens's own
definition page says in as many words that reading it as missing data is wrong —
a blind composer writes *"no bond is recorded … the query returned a null or
placeholder value"* while every verification check passes, so the serve ships as
`certification: certified`, `confidence: verified`. Correct SQL, correct rows,
wrong English, top badge. `governing_definitions` below is the fix: the pages
that govern the projected columns ride the compose prompt, for a few hundred
prompt tokens and no latency cost (a model that knows what a code means writes
less hedging).
`compose_prompt` exists so `dst lens prompt` renders this call too; showing
only the generation half is how a definition could be in "the prompt" and absent
from the call that wrote the sentence it forbade.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from services.contracts.protocols import (
    CacheableBlock,
    ContextChunk,
    GeneratedQuery,
    LLMProvider,
    Message,
)
from services.contracts.response import Citation
from services.contracts.semantic_model import Definition, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.assembly import ANSWER_CONTRACT_SOURCE

_SYSTEM = (
    "You write concise, grounded analytics answers (1-3 sentences). "
    "Use ONLY the provided result rows — never invent numbers. A number may come "
    "from exactly two places: the result rows, or the definitions and data notes "
    "provided with them — and a number taken from a definition or data note must "
    "name its source in the sentence ('per the table profile, ~26% of tickets are "
    "unrated'). If a caveat needs a figure you were not given, state the caveat "
    "without a figure. "
    "Write figures in plain decimal notation with thousands separators, copying "
    "the digits from the result exactly — never scientific notation, never a "
    "rounded form presented as the exact figure. "
    "If a business definition was applied, mention it. "
    "The definitions given with the result GOVERN what its values mean: read "
    "every value the way its definition reads it, and never write a sentence a "
    "definition contradicts. A stored code is a value, not a gap — never call "
    "one missing, null, empty or 'not recorded' unless the cell is truly NULL. "
    "Never attach a currency symbol, code or unit (such as $, €, USD or EUR) that "
    "the result and its notes do not state: if a monetary amount is given a "
    "currency below, write it in that currency; if none is stated, write the bare "
    "number and name no currency at all."
)

_DEFS_HEADER = "Definitions governing the result columns (they decide what the values mean):"

_NOTES_HEADER = (
    "Data notes — declared facts about the projected columns (from the table "
    "profile); a number used from here must be attributed 'per the table profile':"
)


@dataclass
class AnswerResult:
    text: str
    citations: list[Citation] = field(default_factory=list)
    confidence: Literal["high", "low"] | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _format_rows(columns: list[str], rows: list[list[object]]) -> str:
    head = " | ".join(columns)
    body = "\n".join(" | ".join(str(v) for v in r) for r in rows)
    return f"{head}\n{body}" if body else head


def bound_column(d: Definition) -> str:
    """The column name a definition is ABOUT, lowercased — '' when it binds to an
    entity rather than a member (`about: connected`), which governs how to JOIN
    the table, not what a value in it means. Public because the prompt preview
    reports which pages can reach this call, and must ask the same question."""
    head, dot, member = (d.about or "").rpartition(".")
    return member.strip().lower() if dot and head.strip() else ""


def governing_definitions(
    semantic_model: SemanticModel,
    columns: Sequence[str],
    definition_used: str | None = None,
) -> list[Definition]:
    """The definitions that decide what the values in ``columns`` MEAN.

    Scope, deliberately: the columns of the result the composer is writing about,
    plus whatever definition the generator says it applied. The composer only
    ever renders the rows it was handed, so a definition bound to a column those
    rows do not carry cannot change a word of the prose — and the whole model is
    not free (a handful of definition pages is easily thousands of tokens on
    every single compose call, most of it SQL-authoring traps a prose writer
    must not follow).

    Binding, in order: a definition's ``about: entity.column`` is authoritative;
    an ``about``-less page (a ``percentage`` page explaining a computed column)
    falls back to its term. Matching is on the bare column name, since a result
    column comes back unqualified.

    NOT capped and NOT truncated. A silently dropped definition is the exact bug
    this function exists to fix — a "never read '-' as missing" trap can sit at
    the BOTTOM of its page — so the count is bounded by how many of the
    lens's definitions bind to columns the answer actually projects, and nothing
    is ever cut to fit.
    """
    wanted = {str(c).strip().lower().rpartition(".")[2] for c in columns}
    return [
        d
        for d in semantic_model.definitions
        if d.term == definition_used or (bound_column(d) or d.term.strip().lower()) in wanted
    ]


def declared_currency(semantic_model: SemanticModel, columns: Sequence[str]) -> str | None:
    """The currency the author DECLARED for the monetary columns this answer
    projects — never a guess. Matches a result column to the metric of the same
    name (metrics come back aliased to their name, as `governing_definitions`
    relies on) and reads its ``currency``.

    Returns None when nothing is declared, and — deliberately — also when two
    projected metrics declare DIFFERENT currencies: the composer must not assert
    one currency over mixed money, and a bare number beats a wrong symbol. Every
    monetary answer this repo mis-stamped `$` had NO declaration; None keeps it a
    bare number rather than trading one guess for another.
    """
    wanted = {str(c).strip().lower().rpartition(".")[2] for c in columns}
    found = {
        m.currency.strip().upper()
        for e in semantic_model.entities
        for m in e.metrics
        if m.currency and m.name.strip().lower() in wanted
    }
    return next(iter(found)) if len(found) == 1 else None


def data_notes(semantic_model: SemanticModel, columns: Sequence[str]) -> str:
    """Declared field facts for the columns this answer projects — '' when none.

    The profile-enriched field descriptions (null rate, value range, top values)
    ride the GENERATION prompt, and figures from them leak into prose unlabeled:
    an answer saying "about 26% are excluded … ratings range from 3 to 5" is the
    profile's own `~26% null · range: 3..5` restated as if derived from the result,
    which numeric_grounding rightly flags. Handing the composer those facts
    EXPLICITLY — with the attribution rule in the system prompt — turns the useful
    caveat into a cited one instead of an invention. Scoped to projected columns
    for the same reason `governing_definitions` is: a note about a column the rows
    don't carry cannot change a word of the prose.
    """
    wanted = {str(c).strip().lower().rpartition(".")[2] for c in columns}
    lines = [
        f"- {entity.name}.{field.name}: {field.description}"
        for entity in semantic_model.entities
        for field in entity.fields
        if field.description and field.name.strip().lower() in wanted
    ]
    return "\n".join(lines)


def money_positions(semantic_model: SemanticModel, columns: Sequence[str]) -> set[int]:
    """Indices of result columns the semantic model DECLARES monetary — a metric
    of the same name with ``format: currency`` or a ``currency`` code (metrics
    come back aliased to their name, the `governing_definitions` invariant).
    Declared only, never inferred: an undeclared column renders as stored."""
    money = {
        m.name.strip().lower()
        for e in semantic_model.entities
        for m in e.metrics
        if m.format == "currency" or m.currency
    }
    return {i for i, c in enumerate(columns) if str(c).strip().lower().rpartition(".")[2] in money}


def _frame_cell(value: object, money: bool) -> str:
    """One cell of the deterministic frame. Money renders at 2dp — the
    float-noise fix falls out here: 1234.5600000000001 is the
    machine's representation, 1234.56 is the answer."""
    if value is None:
        return "NULL"
    if money and isinstance(value, int | float | decimal.Decimal) and not isinstance(value, bool):
        return f"{value:.2f}"
    return str(value)


def certified_frame(
    question: str,
    result: QueryResult,
    semantic_model: SemanticModel,
    *,
    max_rows: int = 200,
    row_count_exact: bool = True,
) -> str:
    """The deterministic renderer: the answer text for a certified TEMPLATE
    serve (and any certified serve that must not compose freely) — the question
    restated over the result as a compact table line set, written entirely by
    code. No model, no numeric_grounding pass to fail, byte-identical on every
    serve of the same result. Formatting comes from the semantic model's own
    declarations: monetary columns render at 2dp, everything else as stored."""
    n = len(result.rows)
    count = f"more than {n}" if not row_count_exact else str(n)
    noun = "row" if n == 1 and row_count_exact else "rows"
    money = money_positions(semantic_model, result.columns)
    shown = result.rows[:max_rows]
    body = "\n".join(
        " | ".join(_frame_cell(v, i in money) for i, v in enumerate(row)) for row in shown
    )
    head = " | ".join(result.columns)
    table = f"{head}\n{body}" if body else head
    tail = f"\n(showing the first {max_rows} of {count} rows)" if n > max_rows else ""
    return f"{question} — {count} result {noun}:\n{table}{tail}"


def render_definitions(defs: Sequence[Definition]) -> str:
    """The governed pages as the composer is shown them — the generator's own line
    shape minus ``[sql:]``, ``grain`` and ``sources``, which shape SQL and would
    only invite a prose writer to quote query mechanics at the reader."""
    lines = [_DEFS_HEADER]
    for d in defs:
        about = f" (about {d.about})" if d.about else ""
        summary = f" — {d.summary}" if d.summary else ""
        lines.append(f"- {d.term}{about}{summary}: {d.body}")
    return "\n".join(lines)


def citations_for(generated: GeneratedQuery, prose_context: list[ContextChunk]) -> list[Citation]:
    """The applied definition, the served SQL, and the prose sources generation drew on.

    Deterministic — no model reads or writes it. It sits OUTSIDE the composer because
    citations are governance, not prose: a rows-only serve (``composer=None``) skips
    the prose and must still hand the caller the same provenance.
    """
    citations: list[Citation] = []
    if generated.definition_used:
        citations.append(Citation(type="definition", ref=generated.definition_used))
    citations.append(Citation(type="sql", ref=generated.sql))
    # The answer-contract block rides prose_context into generation but is
    # an instruction, not a source the answer drew on — never a citation.
    citations += [
        Citation(type="context", ref=c.source)
        for c in prose_context
        if c.source != ANSWER_CONTRACT_SOURCE
    ]
    return citations


def compose_prompt(
    *,
    question: str,
    generated: GeneratedQuery,
    result: QueryResult,
    semantic_model: SemanticModel,
    max_rows: int = 200,
    row_count_exact: bool = True,
    feedback: str | None = None,
) -> tuple[str, str]:
    """The composer's (system, user turn) — the second model call, verbatim.

    ``feedback`` is the figure gate's one retry: the numeric_grounding failure
    reason, named in-prompt so the second attempt knows exactly which figure it
    must not restate. Appended by code, never by a model.

    Split out of `compose()` so `dst lens prompt` renders the SAME text the
    model is sent rather than a second description of it. The surface that exists
    to answer "what does the model actually see" showed only the generation half
    for as long as this call had no renderer, which is how a definition that WAS
    in the generation prompt could be absent here with nothing to show it.
    """
    # `max_rows` is the PROMPT budget, not the caller's payload cap (the
    # pipeline owns that one and states it in the answer): say plainly how
    # much of the result these rows are, so a summary written from a slice
    # never reads as a summary of everything.
    truncated = len(result.rows) > max_rows
    rows_text = _format_rows(result.columns, result.rows[:max_rows])
    seen = f"showing the first {max_rows}" if truncated else "all shown"
    # Past the fetch cap the count is a floor, so the prompt must not hand the
    # model an exact total to write into prose ("all 5000 customers" when the
    # query matched 21,056).
    count = f"{len(result.rows)}+" if not row_count_exact else str(len(result.rows))
    defs = governing_definitions(semantic_model, result.columns, generated.definition_used)
    block = f"{render_definitions(defs)}\n" if defs else ""
    notes = data_notes(semantic_model, result.columns)
    notes_block = f"{_NOTES_HEADER}\n{notes}\n" if notes else ""
    currency = declared_currency(semantic_model, result.columns)
    money = (
        f"Monetary amounts are in {currency}; write them in that currency.\n" if currency else ""
    )
    clock = (
        f"Dates and day boundaries are {semantic_model.timezone}; say so if the answer "
        f"depends on a period boundary.\n"
        if semantic_model.timezone
        else ""
    )
    # The freshness contract steers what the composer may CLAIM, not what it
    # computes: measured freshness (data_as_of) is a response-only trust signal
    # the model never sees, so the one wrong move here is inventing one.
    fresh = (
        f"This lens declares data stale after {semantic_model.stale_after_days} days; "
        f"actual freshness is measured and reported by the service — do not assert "
        f"how current the data is.\n"
        if semantic_model.stale_after_days
        else ""
    )
    fix = (
        f"A previous answer was rejected: {feedback}. Every figure must come from "
        "the result rows, the definitions, or the data notes above — if a figure "
        "is not there, state the caveat without it.\n"
        if feedback
        else ""
    )
    # The scope rides the composer too: a scoped-subset number
    # presented as the whole population is exactly the quiet-wrong shape, and
    # the prose is where a reader meets it first.
    import re as _re

    scoped = [
        e.population
        for e in semantic_model.entities
        if e.population
        and _re.search(
            rf"\b{_re.escape(e.name)}\b|\b{_re.escape(e.source.table.split('.')[-1])}\b",
            generated.sql,
            _re.IGNORECASE,
        )
    ]
    population = (
        "The data covers ONLY " + "; ".join(dict.fromkeys(scoped)) + " — state this "
        "scope in the answer; never present the figure as covering everything.\n"
        if scoped
        else ""
    )
    # A grouped result has no single number to state: asked "how much revenue"
    # over three currency rows, the composer invents a cross-currency total
    # that exists nowhere, and the guard rightly withholds the whole answer —
    # a correct result converted into no answer. The rows ARE
    # the answer; say so before the model reaches for a scalar.
    multirow = (
        "The result has MULTIPLE rows — the rows themselves are the answer. "
        "Report the per-row values (or the subset the question asks about). "
        "NEVER add rows together or state a combined total the rows do not "
        "contain: a sum across groups such as currencies or regions is an "
        "invented number and will be rejected.\n"
        if len(result.rows) > 1
        else ""
    )
    user = (
        f"Question: {question}\n"
        f"SQL: {generated.sql}\n"
        f"Definition applied: {generated.definition_used or 'n/a'}\n"
        f"{block}"
        f"{notes_block}"
        f"{money}"
        f"{clock}"
        f"{fresh}"
        f"{population}"
        f"{multirow}"
        f"Result ({count} rows, {seen}):\n{rows_text}\n\n"
        f"{fix}"
        "Write the answer."
    )
    return _SYSTEM, user


class AnswerComposer:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        model: str = "claude-sonnet-4-6",
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> None:
        self._llm = llm
        self.model = model  # public: the pipeline reads it to attribute cost per call
        self._temperature = temperature
        self._max_tokens = max_tokens

    def compose(
        self,
        *,
        question: str,
        generated: GeneratedQuery,
        result: QueryResult,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        max_rows: int = 200,
        row_count_exact: bool = True,
        feedback: str | None = None,
    ) -> AnswerResult:
        system, user = compose_prompt(
            question=question,
            generated=generated,
            result=result,
            semantic_model=semantic_model,
            max_rows=max_rows,
            row_count_exact=row_count_exact,
            feedback=feedback,
        )
        res = self._llm.complete(
            # Only the fixed instructions are cacheable — the governed definitions
            # vary with the result's columns, so they ride the user turn and a
            # per-question block never busts the cached prefix.
            system=[CacheableBlock(system)],
            messages=[Message("user", user)],
            model=self.model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        truncated = len(result.rows) > max_rows
        confidence: Literal["high", "low"] = (
            "low" if (truncated or not result.rows or not generated.definition_used) else "high"
        )
        return AnswerResult(
            text=res.text.strip(),
            citations=citations_for(generated, prose_context),
            confidence=confidence,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
        )
