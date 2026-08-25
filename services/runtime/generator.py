"""Grounded SQL generator — turns a question into SQL bound to the semantic model.

v1 of the `QueryGenerator` seam: the full structured semantic model is injected
deterministically into a (prompt-cached) system prompt, prose context is appended to
the user turn, and the LLM returns a JSON object with the SQL. sql_guard
validates the result before execution. A future semantic-query compiler can replace
this behind the same interface.
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
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.runtime.compiler import CompileError, metric_sql
from services.runtime.identifiers import quote_ident, quote_table

_RULES = (
    "You translate a question into a SINGLE read-only SQL SELECT for the given dialect.\n"
    "Rules:\n"
    "- Use ONLY the tables and columns listed in the model; never reference anything else.\n"
    "- Tables are aliased to their entity names (FROM <table> AS <entity>); qualify\n"
    "  columns by entity name. Copy every name from the model EXACTLY as listed,\n"
    "  including its quotes — a quoted name is a name that is also a SQL keyword,\n"
    "  and unquoting it makes the statement unparseable.\n"
    "- SELECT only — no DDL/DML. Do not use SELECT * ; list explicit columns.\n"
    "- Apply the listed business definitions when relevant. When a definition lists an\n"
    "  [sql: <expr>], that expression IS the definition: embed it EXACTLY in your SQL —\n"
    "  do not re-derive or hand-roll the calculation, and when the prose description\n"
    "  disagrees with the [sql: <expr>], the [sql: <expr>] wins (prose can lag an\n"
    "  edit). Report the term you used in definition_used.\n"
    "- When computing a metric that lists 'REQUIRES filters', AND those exact\n"
    "  conditions into your WHERE clause — they are part of the metric's meaning.\n"
    "- A 'dimension' is a named expression over the entity's columns (a grouping or\n"
    "  label the curator authored): when the question asks for that concept, embed\n"
    "  the dimension's listed expression EXACTLY and honour its description — do\n"
    "  not re-derive the expression from raw columns.\n"
    "- Computing a ratio or metric the model does not define is OUT OF SCOPE —\n"
    '  refuse with {"no_answer": ...} naming the missing metric rather than\n'
    "  deriving it from raw columns.\n"
    "- For trend / over-time questions, bucket and group by the entity's 'time:'\n"
    "  field (date_trunc to the asked period, or the dialect's equivalent); a\n"
    "  metric's [time: <field>] annotation overrides it for that metric.\n"
    "- Respect join relationships: aggregating across a one_to_many join fans out\n"
    "  rows — deduplicate or pre-aggregate before summing.\n"
    "- If the CATEGORY of data the question needs does not exist in this model — no\n"
    "  table covers that domain (e.g. support tickets when no ticketing table is\n"
    "  listed) — do NOT fabricate a query against unrelated tables. A zero from the\n"
    "  wrong table is a wrong answer, not an answer. Respond instead with:\n"
    '  {"no_answer": "<what data is missing, in one sentence>"}\n'
    "- But if the question is about a SPECIFIC entity (a customer, a rep, a product)\n"
    "  and the right tables exist, always write the query: an empty or zero result is\n"
    "  a valid answer. Never refuse just because a name looks unfamiliar.\n"
    "- If the question USES a term marked [AMBIGUOUS], you MUST respond with\n"
    '  {"clarify": {"term": "<term>", "question": "<one question naming the options>",\n'
    '   "options": ["<option>", "..."]}}\n'
    "  instead of SQL — guessing an ambiguous governed term is a wrong answer. Skip\n"
    "  the clarify ONLY when the question's own wording explicitly matches exactly\n"
    "  one listed option. Naming an entity does NOT pick a meaning: 'average value\n"
    "  of a customer' still clarifies when 'value' is ambiguous, while 'average\n"
    "  lifetime value' explicitly picks that option. When unsure, clarify.\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"sql": "<sql>", "definition_used": "<term or null>", "rationale": "<short>"}'
)

# Per-dialect hints appended to the system prompt so generated SQL matches the
# warehouse's syntax. Keyed by SemanticModel.dialect (== the sqlglot dialect name).
_DIALECT_NOTES: dict[str, str] = {
    "postgres": (
        "PostgreSQL dialect: use double-quotes for identifiers, ILIKE for "
        "case-insensitive match, date_trunc('month', col) for period bucketing, "
        "and LIMIT n for row caps. Cast with col::type."
    ),
    "mysql": (
        "MySQL dialect: use backticks for identifiers, LIKE is case-insensitive on "
        "default collations, DATE_FORMAT(col, '%Y-%m-01') or DATE(col) for period "
        "bucketing, and LIMIT n for row caps. Avoid full-text functions unless modeled."
    ),
    "snowflake": (
        "Snowflake dialect: identifiers are case-insensitive unless double-quoted "
        "(uppercase by default), use ILIKE for case-insensitive match, "
        "DATE_TRUNC('month', col) for period bucketing, and LIMIT n for row caps. "
        "Cast with col::type or CAST(col AS type)."
    ),
    "duckdb": (
        "DuckDB dialect: use double-quotes for identifiers (unquoted ones are "
        "case-insensitive), date_trunc('month', col) for period bucketing, and "
        "LIMIT n for row caps. CTEs, window functions, QUALIFY, aggregate "
        "FILTER (WHERE ...) and DISTINCT ON are all available — reach for them "
        "instead of flattening a multi-step question into one pass. "
        "Prefer TRY_CAST over CAST when a column is stored as text: it yields "
        "NULL rather than failing the whole query. Cast before arithmetic, "
        "comparison or ordering on text-stored numbers and dates, cast BLOB to "
        "VARCHAR before projecting or grouping it, and concatenate with || after "
        "CAST(x AS VARCHAR). Parse text dates with strptime(col, format) and "
        "render them with strftime. Integer division returns a DOUBLE."
    ),
    "bigquery": (
        "BigQuery standard SQL: use backticks for identifiers (they are "
        "case-sensitive), DATE_TRUNC(col, MONTH) for period bucketing — note the "
        "argument order is the reverse of other dialects — and LIMIT n for row "
        "caps. Prefer SAFE_CAST over CAST so a bad value yields NULL rather than "
        "failing the query. CTEs, window functions and QUALIFY are available."
    ),
}


def business_today(timezone: str | None) -> str:
    """Today's date on the lens clock, ISO — the fact the generator must be TOLD.

    dst computes the clock for grading (verification.clock_years); without
    telling the model, 'this year' resolves against the model's own training
    prior, and no authoring lever fixes it — explicit instructions, a prepended
    date and in-context exemplars all leave the stale year literal standing.
    Dropped and wrong windows are the single largest wrong-answer source, and
    they serve verified: the number IS in the rows, just for the wrong
    period."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    try:
        now = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now(UTC)
    except Exception:
        now = datetime.now(UTC)
    return now.date().isoformat()


def serialize_model(m: SemanticModel) -> str:
    lines = [f"Dialect: {m.dialect}"]
    # The clock rides on BOTH generation prompts (this one and the metric
    # layer's serialize_layer) — see business_today. Prefer CURRENT_DATE
    # arithmetic in SQL; the literal is here so relative windows can never
    # resolve against the model's training-time prior.
    lines.append(
        f"Today: {business_today(m.timezone)} — resolve 'this year', 'last "
        f"month', 'this quarter' against THIS date (prefer CURRENT_DATE "
        f"arithmetic in SQL), never against your own prior. For 'now'/'current' "
        f"against a snapshot or status-date column, filter to the LATEST "
        f"AVAILABLE date (col = (SELECT MAX(col) FROM …)), never to today's "
        f"literal date — snapshots lag, and today's literal matches nothing."
    )
    if m.timezone:
        lines.append(
            f"Timezone: {m.timezone} — 'today', 'yesterday' and period "
            f"boundaries are this zone's, unless the question names another."
        )
    if m.stale_after_days:
        lines.append(
            f"Freshness contract: data is considered stale {m.stale_after_days} days after "
            f"its last update; the service measures and reports actual freshness — never "
            f"assert the data is current."
        )
    lines += ["", "Tables:"]
    for e in m.entities:
        # Physical translation is the compiler's job: the table is
        # presented AS its entity alias, so entity-qualified member references
        # below are valid SQL verbatim — the LLM never resolves name vs table.
        # Verbatim means QUOTED where a bare name would be misread: an entity
        # named `order` handed to the model bare comes back as `FROM orders AS
        # order`, and the warehouse rejects every answer through it.
        ename = quote_ident(e.name, m.dialect)
        table = quote_table(e.source.table, m.dialect)
        alias = "" if e.source.table == e.name else f" AS {ename}"
        head = f"- {table}{alias}"
        if e.description:
            head += f"  # {e.description}"
        lines.append(head)
        if e.grain:
            lines.append(f"    grain: {e.grain}")
        if e.population:
            # The scoped-subset rail — same line as the metric layer's, so the
            # fact reaches both tiers.
            lines.append(
                f"    population: ONLY {e.population} — state this scope in every "
                "answer; 'all'/'total' beyond it is unanswerable from this table"
            )
        if e.population_filter:
            lines.append(f"    required filter on every query: {e.population_filter}")
        if e.pinned_dimensions:
            lines.append(
                f"    pinned dimensions: {', '.join(e.pinned_dimensions)} — pin to one "
                "value or GROUP BY before any aggregate; a SUM across them is meaningless"
            )
        if e.default_time_field:
            lines.append(f"    time: {e.default_time_field}")
        for u in e.use_cases:
            lines.append(f"    use: {u}")
        for q in e.common_questions[:5]:
            lines.append(f"    answers: {q}")
        for f in e.fields:
            desc = f" — {f.description}" if f.description else ""
            lines.append(f"    {ename}.{quote_ident(f.name, m.dialect)} {f.type}{desc}")
        for dim in e.dimensions:
            # Authored dimensions parse, validate and compile — and would
            # otherwise stop there, invisible to the raw-SQL path while the
            # scaffold instructs you to author them, so a load-bearing
            # value-shape rule in a dimension description is a silent no-op. The
            # expression defaults to the field of the same name, exactly as the
            # intent compiler resolves it (compiler._resolve_dim).
            dname = quote_ident(dim.name, m.dialect)
            expr = dim.expr or f"{ename}.{dname}"
            desc = f" — {dim.description}" if dim.description else ""
            lines.append(f"    dimension {ename}.{dname} {dim.type} = {expr}{desc}")
        for metric in e.metrics:
            try:
                # Simple metrics keep filters in the REQUIRES line below; ratio/derived
                # expansions self-contain every referenced metric's filters.
                sql = metric_sql(metric, e, inline_filters=False)
            except CompileError:
                sql = None  # invalid refs are a validate-time error; don't crash serving
            line = f"    metric {metric.name}"
            if sql:
                line += f" = {sql}"
            if metric.format:
                line += f" [format: {metric.format}]"
            if metric.agg_time_field:
                line += f" [time: {metric.agg_time_field}]"
            if metric.description:
                line += f"  # {metric.description}"
            lines.append(line)
            if metric.filters:
                lines.append(
                    f"    metric {metric.name} REQUIRES filters: {' AND '.join(metric.filters)}"
                )
    if m.joins:
        lines += ["", "Joins:"] + [
            f"- {j.on} ({j.type}{', ' + j.relationship if j.relationship else ''})" for j in m.joins
        ]
    if m.definitions:
        lines += ["", "Definitions:"]
        for d in m.definitions:
            if d.status == "ambiguous":
                options = (
                    f" Options: {'; '.join(d.possible_mappings)}" if d.possible_mappings else ""
                )
                lines.append(
                    f"- {d.term} [AMBIGUOUS — MUST clarify, never guess]: {d.body}{options}"
                )
                continue
            about = f" (about {d.about})" if d.about else ""
            expr = f"  [sql: {d.sql_expr}]" if d.sql_expr else ""
            summary = f" — {d.summary}" if d.summary else ""
            lines.append(f"- {d.term}{about}{summary}: {d.body}{expr}")
            # Same two lines certdefs.render_context emits for a page under
            # model.certified_dir. The identical file under semantic/definitions/
            # used to render neither, because the keys were dropped at parse —
            # one file format cannot mean two things by which directory it is in.
            if d.grain:
                lines.append(
                    f"    grain: {d.grain} (aggregate at this grain; dedupe before summing)"
                )
            if d.sources:
                lines.append(f"    sources: {', '.join(d.sources)}")
    if m.sample_queries:
        lines += ["", "Sample queries:"]
        for s in m.sample_queries:
            lines.append(f"- Q: {s.question}\n  SQL: {s.sql}")
    if m.ai_instructions:
        lines += ["", f"Instructions: {m.ai_instructions}"]
    note = _DIALECT_NOTES.get(m.dialect)
    if note:
        lines += ["", f"Dialect notes: {note}"]
    return _RULES + "\n\n=== Semantic model ===\n" + "\n".join(lines)


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    return t


class GenerationFormatError(ValueError):
    """The model's reply is neither the JSON envelope nor bare SQL.

    The caller re-asks with the problem named instead of the old behavior —
    silently treating arbitrary prose as SQL and burning the pipeline's one
    repair on a formatting slip."""


_BARE_SQL = re.compile(r"(?is)^\s*(select|with)\b")


def parse_generation(text: str) -> GeneratedQuery:
    body = _strip_fences(text)
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        if _BARE_SQL.match(body):
            # A bare SELECT without the envelope is a common model slip and
            # perfectly usable — accept it (rationale/definition_used lost).
            return GeneratedQuery(sql=body)
        raise GenerationFormatError("the reply is neither the JSON object nor SQL") from None
    if not isinstance(obj, dict):
        raise GenerationFormatError("the reply is JSON but not an object")
    if obj.get("no_answer"):
        return GeneratedQuery(sql="", no_answer_reason=str(obj["no_answer"]).strip())
    if isinstance(obj.get("clarify"), dict):
        c = obj["clarify"]
        return GeneratedQuery(
            sql="",
            clarification=ClarificationRequest(
                term=str(c.get("term") or "").strip(),
                question=str(c.get("question") or "").strip(),
                options=[str(o) for o in c.get("options") or []],
            ),
        )
    sql = obj.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise GenerationFormatError('the JSON object carries no usable "sql" string')
    return GeneratedQuery(
        sql=sql.strip(),
        rationale=obj.get("rationale"),
        definition_used=obj.get("definition_used"),
    )


class FixedSQLGenerator:
    """A `QueryGenerator` that returns a pre-approved SQL string (a certified answer),
    skipping the LLM entirely. Consumes no tokens; still passes through guard + execute."""

    def __init__(self, sql: str, *, definition_used: str | None = None) -> None:
        self._sql = sql
        self._definition_used = definition_used
        self.model = "certified"

    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery:
        return GeneratedQuery(sql=self._sql, definition_used=self._definition_used)


class GroundedSQLGenerator:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        model: str = "claude-sonnet-4-6",
        temperature: float = 0.0,
        # Reasoning-mode models bill their thinking against max_tokens BEFORE any
        # content lands: at 1024 a fast-tier model returns EMPTY SQL for every
        # multi-step question (0 chars at 1024, valid SQL at 8192), and the guard
        # reports it as "exactly one statement is allowed". A cap is not a spend.
        max_tokens: int = 8192,
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
        # The serialized model is a fixed per-lens prefix → cache it with a 1h TTL so it
        # stays warm across callers (cache reads are ~0.1x input price).
        system = CacheableBlock(serialize_model(semantic_model), ttl="1h")
        parts: list[str] = []
        if prose_context:
            prose = "\n\n".join(f"[{c.source}] {c.text}" for c in prose_context)
            parts.append(f"Context:\n{prose}")
        parts.append(f"Question: {question}")
        if feedback:
            # Execution-guided self-repair: tell the model exactly why the last try failed.
            parts.append(
                f"Your previous attempt failed and must be corrected. {feedback}\n"
                "Return a corrected query that stays within the model's tables and columns."
            )
        messages = [Message("user", "\n\n".join(parts))]
        in_tok = out_tok = 0
        gq: GeneratedQuery | None = None
        last_text = ""
        for _attempt in range(3):  # initial call + up to 2 format re-asks
            res = self._llm.complete(
                system=[system],
                messages=messages,
                model=self.model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            in_tok += res.input_tokens
            out_tok += res.output_tokens
            last_text = res.text
            try:
                gq = parse_generation(res.text)
                break
            except GenerationFormatError as exc:
                # Appending turns keeps the cached system prefix intact.
                messages.append(Message("assistant", res.text))
                messages.append(
                    Message(
                        "user",
                        f"Invalid reply: {exc}. Respond with ONLY the JSON object "
                        "described in the rules — no prose, no code fences.",
                    )
                )
        if gq is None:
            # Format never converged: hand the reply to sql_guard, which rejects
            # it with feedback into the pipeline's repair/escalate path.
            gq = GeneratedQuery(sql=_strip_fences(last_text))
        gq.input_tokens = in_tok
        gq.output_tokens = out_tok
        return gq
