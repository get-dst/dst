"""The seam protocols every implementation binds to.

`Connector` (warehouse), `LLMProvider` (Claude), `Embedder` (openai-compat/local), and
`QueryGenerator` (grounded SQL). Owned here — implementations import, don't edit.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    JoinCandidate,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.query_intent import QueryIntent
from services.contracts.resolution import DecisionRecord
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot


# ── LLM value types ──────────────────────────────────────────────────────────
@dataclass
class CacheableBlock:
    text: str
    cache: bool = True
    ttl: Literal["5m", "1h"] = "5m"  # cache lifetime; "1h" suits a fixed per-lens prefix


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # The part of cache_creation_tokens written to the long-lived cache
    # (Anthropic's 1-hour, OpenAI's 12-hour), billed at its own rate.
    cache_write_long_tokens: int = 0


# ── Context / generation value types ─────────────────────────────────────────
@dataclass
class ContextChunk:
    text: str
    source: str
    locator: str | None = None
    score: float = 0.0


@dataclass
class GeneratedQuery:
    sql: str
    rationale: str | None = None
    definition_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Set when the generator declines because the model's tables cannot answer
    # the question (the data does not exist in this lens). A named gap — the
    # absence signal — not a failure. sql is empty when set.
    no_answer_reason: str | None = None
    # Set when the question hinges on an AMBIGUOUS governed term: the caller must
    # pick a meaning before SQL exists. sql is empty when set.
    clarification: ClarificationRequest | None = None
    # The compiled QueryIntent when the SQL was built BY CONSTRUCTION from names
    # in the semantic model (intent tier, metrics door). None on raw-SQL paths —
    # the ledger then attributes from the served SQL instead. Read this, not
    # generator_tier: the tier is decided before generation and stays "intent"
    # on a serve the grounded escalation actually wrote.
    intent: QueryIntent | None = None
    # The typed resolver's decisions (each slot's measured choice) and the filter
    # columns whose value the caller supplied — they ride the answer's ledger.
    decisions: list[DecisionRecord] = field(default_factory=list)
    # Decisions a batched call answered ahead that the resolution never
    # reached: off the ledger, but the provider billed them, so their usage
    # is charged with the rest.
    unused_decisions: list[DecisionRecord] = field(default_factory=list)
    supplied: list[str] = field(default_factory=list)
    # True iff the typed resolver produced this SQL (no escalation).
    typed: bool | None = None
    # Parts of the question this SQL does not answer, each "<what>: <why>":
    # the typed resolver served a listing without a field it could not place
    # rather than clarify the whole question. The pipeline relays them on
    # the response's `declined` list.
    declined: list[str] = field(default_factory=list)


# ── Protocols ────────────────────────────────────────────────────────────────
@runtime_checkable
class Connector(Protocol):
    kind: str

    def introspect(self) -> SchemaSnapshot: ...

    def dry_run(self, sql: str) -> DryRunResult: ...

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult: ...


@runtime_checkable
class SharedSnapshot(Protocol):
    """A connector that can hold one read snapshot open across several queries.

    Entering the context yields a connector whose every ``execute`` — each on
    its own connection, so they may run concurrently — reads the same committed
    state of the warehouse, whatever commits in between. A batch of metric
    reads rendered together uses it, so a parent and its children never come
    from two different moments. A connector without it reads each query at its
    own moment, and the batch says so."""

    def shared_snapshot(self) -> AbstractContextManager[Connector]: ...


@runtime_checkable
class TargetedIntrospect(Protocol):
    """A connector that can resolve a requested table subset against the FULL
    catalog, before any listing cap.

    An optional capability for wide warehouses: `introspect()` may cap its
    listing (and mark the snapshot `truncated`), which used to make `--tables`
    match against the capped universe — no table outside it was reachable by
    any spelling. Names match at any qualification depth
    (`table`, `dataset.table`, `project.dataset.table`)."""

    def introspect_tables(self, wanted: list[str]) -> SchemaSnapshot: ...


@runtime_checkable
class WriteProbe(Protocol):
    """A connector that can prove write access by creating + dropping a scratch table.

    Used by connection evaluation (mgmt) when a connection requests write access; the
    probe is the only definitive proof the credential can actually write. It must leave
    nothing behind (always drops its throwaway table).
    """

    def probe_write(self) -> None: ...


@runtime_checkable
class CatalogProfiler(Protocol):
    """A connector that can profile tables from the engine's own catalog.

    Cheap metadata only — descriptions, partitioning/clustering, row counts,
    last-updated, catalog-native column stats — never a table scan. An optional
    capability (like WriteProbe): a connector without it still gets a basic
    profile derived from ``introspect()`` via
    `services.contracts.profile.profiles_from_snapshot`, so adding this protocol
    breaks no existing `Connector` implementation.
    """

    def profile_catalog(self) -> list[TableProfile]:
        """Profile every visible table from catalog metadata. No table scans."""
        ...

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """Join keys the engine itself declares (foreign keys); [] where none exist."""
        ...


@runtime_checkable
class SamplingProfiler(Protocol):
    """A connector that can sample table data under hard guards.

    An optional capability (like CatalogProfiler): the sampling pass fills what
    the catalog pass couldn't — observed null rates, cardinality
    (APPROX_COUNT_DISTINCT where the dialect has it), min/max for time + numeric
    columns, enum literals (``top_values``) for low-cardinality columns, and a
    MAX(partition/time column) probe for ``last_updated_logical``.

    Implementations must keep every query read-only and capped: at most
    ``max_rows`` rows per table via the dialect's sampling clause (USING SAMPLE /
    TABLESAMPLE / SAMPLE / LIMIT), plus a bytes budget where the engine bills
    scans (BigQuery dry-runs first and refuses over-budget queries). A column
    whose spec says ``shape_only`` never has literal values collected — shape
    (nulls/cardinality) only. A table that cannot be sampled within budget is
    omitted from the result so its catalog profile stands.
    """

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Sample the specified tables/columns; profiles carry ``source="sampled"``."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult: ...


# ── Typed decisions ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Option:
    """One member of a closed option set a decision is made over. ``aliases``
    are the further spellings a question may name it by, word for word (a
    metric's label and aliases); the decider sees the name and description."""

    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()


@dataclass
class DecisionUsage:
    """What one decision cost: provider calls (fractional when several
    decisions rode one call), tokens as the provider reports them, and the
    wall time of the call(s). Summed per resolution, priced per provider."""

    calls: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    latency_s: float = 0.0

    def split(self, n: int) -> list[DecisionUsage]:
        """One call's usage shared by the *n* decisions it answered. Tokens
        are split whole, the remainder to the first shares, so the shares
        add up to what the provider billed: a fractional share rounded on
        each answer row would lose a token per decision."""
        n = max(1, n)

        def whole(total: float) -> list[int]:
            q, r = divmod(round(total), n)
            return [q + (i < r) for i in range(n)]

        return [
            DecisionUsage(
                calls=self.calls / n,
                input_tokens=float(i),
                output_tokens=float(o),
                latency_s=self.latency_s / n,
            )
            for i, o in zip(whole(self.input_tokens), whole(self.output_tokens), strict=True)
        ]

    def as_dict(self) -> dict[str, float]:
        return {
            "calls": round(self.calls, 4),
            "input_tokens": round(self.input_tokens, 2),
            "output_tokens": round(self.output_tokens, 2),
            "latency_s": round(self.latency_s, 4),
        }


# What a failure billed before it failed: ``(provider, usage)`` pairs riding
# the exception. A decider whose calls were billed and then failed (a vote
# that errored after others answered, a reply that came back unreadable), and
# a resolution that fails after some of its decisions were made, attach their
# spend here on the way out, so whoever catches the failure charges it.


def spend_on(exc: BaseException, provider: str, usage: DecisionUsage) -> None:
    """Attach *usage*, billed by *provider*, to the failure *exc*."""
    if usage.calls or usage.input_tokens or usage.output_tokens:
        exc.spent = [*spent_by(exc), (provider, usage)]  # type: ignore[attr-defined]


def spent_by(exc: BaseException) -> list[tuple[str, DecisionUsage]]:
    """The spend attached to *exc* (``spend_on``); empty when none was."""
    return list(getattr(exc, "spent", None) or [])


@dataclass
class Decision:
    """A closed-set choice with the probability the decider MEASURED for it.

    ``probs`` is None when nothing was measured — a single sample, a provider
    with no way to expose likelihoods. It is never a fabricated 1.0: a policy
    reading None falls back to act-or-decline, and the calibration report
    prints that decider as UNTESTED instead of a number. ``chosen`` is None for
    the ``none`` option (no member fits), and then ``probs`` carries "none".
    """

    chosen: str | None
    probs: dict[str, float] | None
    provider: str
    # What the decision cost — None when the decider did not measure it.
    usage: DecisionUsage | None = None
    # The vote behind ``probs`` when the decider sampled: how many of the ``k``
    # samples chose ``chosen``. None for a decider that measured another way
    # (a probability wire) or measured nothing.
    votes: int | None = None
    k: int | None = None
    # The provider declined to answer (a typed refusal): ``chosen`` is None and
    # nothing was measured, so the slot declines; never read as a pick.
    refused: bool = False

    @property
    def p(self) -> float | None:
        if self.probs is None:
            return None
        return self.probs.get(self.chosen if self.chosen is not None else "none", 0.0)

    @property
    def runner_up(self) -> float | None:
        if self.probs is None:
            return None
        key = self.chosen if self.chosen is not None else "none"
        rest = [v for k, v in self.probs.items() if k != key]
        return max(rest) if rest else 0.0

    @property
    def margin(self) -> float | None:
        p, r = self.p, self.runner_up
        return None if p is None or r is None else p - r

    @property
    def p_lower(self) -> float | None:
        """The disclosed bound under the vote: Laplace's rule of succession,
        (votes + 1) / (k + 2). Five of five is 0.857, not the 1.0 the vote share
        reads as — a unanimous sample of five is not certainty. None when no
        vote was taken."""
        if self.votes is None or self.k is None:
            return None
        return (self.votes + 1) / (self.k + 2)


@runtime_checkable
class Decider(Protocol):
    """A closed-set decision maker: question + context + options -> Decision.

    The seam a typed-decision model plugs into 1:1 (fast, cheap, calibrated
    classification over options declared in advance). Today's implementation
    votes a chat model; the option-set builder and the threshold policy are the
    durable halves either way.
    """

    def decide(
        self,
        *,
        question: str,
        context: str,
        options: list[Option],
        allow_none: bool,
    ) -> Decision: ...


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class QueryGenerator(Protocol):
    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery: ...


__all__ = [
    "CacheableBlock",
    "CatalogProfiler",
    "Connector",
    "ContextChunk",
    "Embedder",
    "GeneratedQuery",
    "LLMProvider",
    "LLMResult",
    "Message",
    "QueryGenerator",
    "SamplingProfiler",
    "WriteProbe",
]
