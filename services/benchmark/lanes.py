"""Lanes: the ways an answer gets produced.

- ``BaselineLane`` — *no dst*: the model gets a bare schema listing and the
  question, writes SQL, the SQL runs read-only against the artifact. This is
  what "just point an agent at the warehouse" actually is.
- ``DstLane`` — the real runtime pipeline (``services/runtime/pipeline.py``:
  ground → generate → guard → execute → compose), built over the same artifact.
  Features are *stripped* via ``DstFeatures``, so "new dst vs old
  dst" and "dst minus X" are configs, not forks.

Every lane returns the same ``LaneAnswer`` so grading and reporting don't care
who produced the answer.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.contracts.protocols import CacheableBlock, ContextChunk, LLMProvider, Message
from services.contracts.semantic_model import Definition, SemanticModel
from services.lenses.describe import LensDescription, describe_model
from services.observability.cost import ai_cost_usd
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.pipeline import run_query

from .certified import CertifiedIndex
from .questions import Question
from .warehouse import Warehouse, coerce

_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# The declared tolerance under --stale-days: fixed so the experiment has one
# knob (how old the world is), not two. stale_days > this ⇒ contract violated.
STALE_CONTRACT_DAYS = 2


@dataclass(frozen=True)
class LaneAnswer:
    columns: list[str]
    rows: list[list[object]]
    sql: str | None
    error: str | None = None
    # The HAL four-tuple (tokens, dollars, wall-clock, trace id) — the reporting
    # standard from "AI Agents That Matter" (arXiv 2407.01502), which showed
    # trivial retry baselines Pareto-dominating SOTA agents at 50x lower cost.
    # An accuracy number without its cost is not a result.
    ai_cost_usd: float = 0.0
    latency_ms: float = 0.0
    tokens: int = 0
    trace_id: str | None = None
    calls: int = 1  # caller-visible calls (agentic lanes loop; lens lanes = 1)
    # Observability parity with production (which logs full traces): agentic
    # lanes carry their loop transcript so a declined run is explainable.
    transcript: str | None = None
    # The pipeline's own trust label (verified | partial | unverified) — the
    # signal a decline gate would fire on. Recorded so the gate can be PRICED
    # (wrongs converted vs corrects lost) before anything is made to refuse.
    confidence: str | None = None
    # certified | none — whether approved SQL served this answer. An agent that
    # cannot see whether it got a certified answer cannot reason about trusting
    # it, so this rides the observation an agentic lane shows its caller.
    certification: str | None = None
    # Freshness-disclosure signal, set only when the run synthesized a stale
    # world (--stale-days): did this answer's verification carry a failed
    # `freshness` check? None = freshness was not under test in this run.
    stale_disclosed: bool | None = None
    # Agentic lanes: replies the harness could not parse as a protocol step.
    # First-class, because formatting noise read as task difficulty is exactly
    # the mistake this benchmark made once already.
    protocol_failures: int = 0
    # Agentic lanes: calls to tools that cost no LLM call and no warehouse hit
    # (``describe``) — free of the call budget, so their use must stay visible.
    free_calls: int = 0
    # Stage attribution — the delivered PROSE. Production serves the sentence, not the
    # rows; a harness that never carries it can grade rows-correct answers
    # whose narration states different numbers as correct.
    # None = the lane delivers tabular data only (every raw-SQL arm).
    answer: str | None = None
    # Production's own deterministic numeric_grounding verdict over that prose
    # (pass | fail | skip) — carried, not re-derived: the check needs the
    # pipeline's definitions/notes/clock, and running it context-starved
    # harness-side manufactures false positives that check is tuned to avoid.
    grounding: str | None = None
    grounding_reason: str | None = None  # the check's own one-liner, for stage_evidence
    # The lens this answer was ROUTED to, set only by lanes that actually
    # route. Lens-pinned lanes leave it None so the routing stage grades
    # `skipped`, never `passed` — an untested stage is not a clean one.
    lens: str | None = None


class BaselineLane:
    """Schema-only SQL generation, no dst anywhere in the path."""

    name = "baseline"

    def __init__(
        self, llm: LLMProvider, warehouse: Path | Warehouse, schema: str, *, model: str
    ) -> None:
        self._llm = llm
        self._wh = coerce(warehouse)
        self._schema = schema
        self._model = model

    def answer(self, question: Question) -> LaneAnswer:
        started = time.perf_counter()
        prompt = (
            f"You write {self._wh.dialect_word} SQL. Schema (schema.table, columns, types):\n\n"
            f"{self._schema}\n\n"
            "Answer the question with one SQL query. Reply with only the SQL, "
            "no prose. Qualify tables as schema.table."
        )
        result = self._llm.complete(
            system=[CacheableBlock(text=prompt, ttl="1h")],
            messages=[Message(role="user", content=question.question)],
            model=self._model,
            temperature=0.0,
            # Headroom for reasoning-mode models: their thinking bills against
            # max_tokens BEFORE any SQL lands in content — 1024 starved
            # deepseek-v4 on Spider-complexity questions into empty replies.
            max_tokens=8192,
        )
        spent = ai_cost_usd(self._model, result.input_tokens, result.output_tokens) or 0.0

        tokens = result.input_tokens + result.output_tokens

        def _done(**kw: object) -> LaneAnswer:
            ms = round((time.perf_counter() - started) * 1000, 1)
            return LaneAnswer(ai_cost_usd=spent, latency_ms=ms, tokens=tokens, **kw)  # type: ignore[arg-type]

        sql = _extract_sql(result.text)
        if not sql:
            return _done(columns=[], rows=[], sql=None, error="no SQL in model output")
        res = self._wh.execute(sql)
        return _done(columns=res.columns, rows=res.rows, sql=sql, error=res.error)


@dataclass(frozen=True)
class DstFeatures:
    """The stripping surface: each flag maps to a real pipeline capability.
    ``DstFeatures()`` is current-full-dst; strip from there."""

    context: bool = True  # prose context chunks reach the generator
    repair: bool = True  # execution-guided self-repair (max_repairs)
    judge: bool = False  # opt-in inline judge tier — costs a second call
    adversary: bool = False  # opt-in adversarial review — a second opinion
    # The declared freshness contract (stale_after_days + the serve-time check).
    # Stripping it models a lens that never declared one: data_as_of still
    # rides, but nothing compares it to a contract — the pre-TRUST-EV world.
    freshness: bool = True

    def stripped(self, names: list[str]) -> DstFeatures:
        unknown = set(names) - {f for f in self.__dataclass_fields__}
        if unknown:
            raise ValueError(f"unknown features to strip: {sorted(unknown)}")
        return DstFeatures(
            **{f: (False if f in names else getattr(self, f)) for f in self.__dataclass_fields__}
        )


class DstLane:
    """The real runtime pipeline over the same DuckDB artifact."""

    def __init__(
        self,
        llm: LLMProvider,
        warehouse: Path | Warehouse,
        *,
        model: str,
        features: DstFeatures | None = None,
        context_chunks: list[ContextChunk] | None = None,
        caller_registry: dict[str, str] | None = None,
        certified_index: CertifiedIndex | None = None,
        instructions: str | None = None,
        definitions: list[Definition] | None = None,
        table_scope: set[str] | None = None,
        stale_days: int | None = None,
        name: str = "dst",
    ) -> None:
        self.name = name
        self._features = features or DstFeatures()
        wh = coerce(warehouse)
        self._connector = wh.connector()
        # The scoped-lens rung: a lens exposes only the tables its use case
        # needs — smaller context, faster, cheaper, and fewer wrong turns.
        self._semantic_model = wh.structural_model(include=table_scope)
        if instructions:
            # The instructions rung: the lens author's standing orders —
            # ai_instructions is a real SemanticModel field, rendered into the
            # generation prompt by serialize_model.
            self._semantic_model = self._semantic_model.model_copy(
                update={"ai_instructions": instructions}
            )
        if definitions:
            # The authored-definitions rung: business rules as the product
            # artifact (SemanticModel.definitions) — rendered by serialize_model
            # and clarify-aware, exactly as a lens author would ship them.
            self._semantic_model = self._semantic_model.model_copy(
                update={"definitions": list(definitions)}
            )
        # The freshness experiment (--stale-days): a synthesized measured floor,
        # aged `stale_days` back from now. The CONTRACT only exists when the
        # feature isn't stripped — the strip lane keeps the measured floor but
        # never declared a tolerance, exactly the undeclared-lens control.
        self._data_as_of: str | None = None
        if stale_days is not None:
            floor = datetime.now(UTC) - timedelta(days=stale_days)
            self._data_as_of = floor.date().isoformat()
            if self._features.freshness:
                self._semantic_model = self._semantic_model.model_copy(
                    update={"stale_after_days": STALE_CONTRACT_DAYS}
                )
        self._generator = GroundedSQLGenerator(llm, model=model, max_tokens=8192)
        self._composer = AnswerComposer(llm, model=model)
        self._llm = llm
        self._model = model
        self._context = context_chunks or []
        # The runtime-context rung: who is asking. None = rung not present.
        self._caller_registry = caller_registry
        # The certified rung: approved SQL served instead of generation.
        self._certified = certified_index

    @property
    def semantic_model(self) -> SemanticModel:
        return self._semantic_model

    @property
    def prose_context(self) -> list[ContextChunk]:
        return list(self._context)

    @property
    def certified_index(self) -> CertifiedIndex | None:
        return self._certified

    def describe(self) -> LensDescription:
        """What this lens knows — zero LLM calls, zero warehouse hits.

        The same projection ``GET /v1/lenses/{name}`` serves. Agentic lanes hand
        it to the caller so it can read the definitions and the standing orders
        *before* paying 80 seconds for an ask.
        """
        certified = self._certified
        return describe_model(
            self.name,
            display_name=self.name,
            description="the governed lens over this warehouse",
            model=self._semantic_model,
            certified_count=len(certified) if certified else 0,
            certified_examples=[q for q, _ in certified.pairs()[:5]] if certified else [],
        )

    def answer(self, question: Question) -> LaneAnswer:
        certified = self._certified.match(question.question) if self._certified else None
        if certified is not None:
            return self._run(question, FixedSQLGenerator(certified.sql), certification="certified")
        return self._run(question, self._generator, certification="none")

    def _run(self, question: Question, generator: object, *, certification: str) -> LaneAnswer:
        f = self._features
        chunks = list(self._context) if f.context else []
        if self._caller_registry and question.caller:
            note = self._caller_registry.get(question.caller)
            if note:
                chunks.append(
                    ContextChunk(
                        text=f"RUNTIME CONTEXT — the caller asking this question: {note}",
                        source="caller-registry",
                    )
                )
        result = run_query(
            question=question.question,
            lens_name="proving_ground",
            org_id="benchmark",
            caller=question.caller or "benchmark-runner",
            semantic_model=self._semantic_model,
            connector=self._connector,
            generator=generator,  # type: ignore[arg-type]
            composer=self._composer,
            prose_context=chunks,
            max_repairs=1 if f.repair else 0,
            inline_judge_llm=self._llm if f.judge else None,
            judge_model=self._model,
            adversary_llm=self._llm if f.adversary else None,
            adversary_model=self._model,
            certification=certification,  # type: ignore[arg-type]
            data_as_of=self._data_as_of,
        )
        response = result.response
        data = response.data
        cost = result.trace.ai_cost_usd or 0.0
        ms = result.trace.latency_ms.get("total", 0.0) if result.trace.latency_ms else 0.0
        tokens = (result.trace.ai_input_tokens or 0) + (result.trace.ai_output_tokens or 0)
        trace_id = result.trace.request_id
        stale_disclosed: bool | None = None
        if self._data_as_of is not None:
            fresh = response.verification.check("freshness") if response.verification else None
            stale_disclosed = fresh is not None and fresh.status == "fail"
        grounding_check = (
            response.verification.check("numeric_grounding") if response.verification else None
        )
        grounding = grounding_check.status if grounding_check else None
        if data is None or not data.rows:
            return LaneAnswer(
                columns=[],
                rows=[],
                sql=response.sql,
                error=response.answer,
                ai_cost_usd=cost,
                latency_ms=ms,
                tokens=tokens,
                trace_id=trace_id,
                confidence=response.confidence,
                certification=response.certification,
                stale_disclosed=stale_disclosed,
            )
        return LaneAnswer(
            columns=list(data.columns),
            rows=[list(r) for r in data.rows],
            sql=response.sql,
            ai_cost_usd=cost,
            latency_ms=ms,
            tokens=tokens,
            trace_id=trace_id,
            confidence=response.confidence,
            certification=response.certification,
            stale_disclosed=stale_disclosed,
            answer=response.answer,
            grounding=grounding,
            grounding_reason=grounding_check.reason if grounding_check else None,
        )


def _extract_sql(text: str) -> str | None:
    match = _SQL_FENCE.search(text)
    candidate = (match.group(1) if match else text).strip().rstrip(";").strip()
    return candidate or None
