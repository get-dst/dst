"""The coverage gap-map — one gap, three views bound.

The capstone of the lens router: surface area stops being three separate numbers
and becomes one decomposition. A coverage gap is a *metric* the org reasons about
that **no published lens governs**. We see that hole three ways and bind them:

  1. **Router declines** (live) — questions the router could not route to any
     covering lens, clustered into uncovered-metric groups (``surface.cluster_declines``).
     These are surface-area misses: a metric asked-about but ungoverned, *now*.
  2. **Audit ungoverned drift** (documented) — Definition-Drift findings
     (``DriftFinding.metric_intent``): a metric computed several inconsistent ways
     across the warehouse. Each is a metric the org clearly cares about.
  3. **Lens governance** (the answer) — what each published lens OWNS, projected
     from its coverage profile: certified metrics, semantic-model definitions, and
     entity metric names.

The **warehouse-level view** (``build_gap_map``) decomposes every drifting /
uncovered metric into ``governed-by-lens-X`` or ``UNGOVERNED`` (a true gap), de-duped
across the two evidence sources by normalized metric text. The **per-lens view**
(``lens_in_scope_findings``) claims only the slice of findings WITHIN A LENS'S SCOPE
— the governed findings it is accountable to resolve canonically. Out-of-scope
drift is excluded *by construction*: a lens is never shown another scope's failure.

THE BINDING PRINCIPLE (load-bearing) — scope-binding is by **metric governance**,
not raw table overlap. A finding belongs to a lens iff the finding's metric matches
one of the lens's *governed metrics* (certified-definition page / definition / entity metric). It
is deliberately NOT bound by the tables the SQL touches: drift lives in gold marts
a bronze-sourced certified never names, so table overlap would mis-assign findings (a
finance certified sourced from ``bronze.invoices`` would never claim its own revenue
drift, which runs over ``gold.fct_revenue_monthly``). Governance is about the
*metric's identity*, decided over the metric's NAME, the same way the drift miner
clusters variants of one intent across disagreeing tables (``drift._tokens``).

Matching is two-tier, both name-based and embedder-free (so it stays offline-
testable, like the router's ScriptedLLM/Hash path):

  - **normalized exact** (``contamination.normalize``) — the lens governs the metric
    under the same canonical text; and
  - **semantic token-core overlap** (reusing ``drift``'s structural-vocabulary
    stripping + plural stem) — "sum of revenue" (a drift ``metric_intent``) matches
    a governed ``net_invoiced_revenue`` because both reduce to the ``{revenue}``
    core, while ``freight cost`` stays apart.

Resolving a gap (extend / build a lens so it governs the metric) makes the router
hit-rate climb, drops the Audit's ungoverned count, and closes the lens's open
scope — the same fix in all three surfaces, by construction of this binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from services.api.surface import UncoveredCluster
from services.benchmark.contamination import normalize
from services.probe.drift import DriftFinding
from services.probe.drift import _tokens as _semantic_tokens
from services.router import CoverageProfile

# ── metric-governance matching (the load-bearing seam) ───────────────────────


@dataclass(frozen=True)
class GovernedMetric:
    """One metric a lens governs, in matchable form. Built from the lens's coverage
    profile anchors (certified metrics, definitions, entity metrics) — NEVER its tables.

    Carries both keys the matcher uses: the normalized text (exact match) and the
    semantic token-core (structural-vocabulary-stripped, plural-stemmed overlap)."""

    text: str  # the verbatim governed-metric anchor, for display
    norm: str  # contamination.normalize(text) — exact-match key
    core: frozenset[str]  # drift._tokens(text) — semantic overlap key


def _governed_metrics(profile: CoverageProfile) -> list[GovernedMetric]:
    """The set of metrics a lens governs, from its coverage-profile anchors.

    ``anchors`` is exactly the lens's SPECIFIC governed surface — certified
    metrics/questions/summaries, semantic-model definitions, and entity/metric names
    (see ``router.profiles.coverage_profile``). The profile's ``scope`` (tables) is
    intentionally ignored: governance is over the metric's identity, not its storage.
    """
    out: dict[str, GovernedMetric] = {}
    for anchor in profile.anchors:
        norm = normalize(anchor)
        if not norm:
            continue
        out.setdefault(norm, GovernedMetric(text=anchor, norm=norm, core=_semantic_tokens(anchor)))
    return list(out.values())


# Connective words that survive drift's table/column tokenizer (it never sees them
# in physical names) but pollute a metric *label*'s core — "sum of revenue" must
# reduce to {revenue}, not {revenue, of}, so it merges with a "revenue" decline.
_CONNECTIVES = frozenset({"of", "per", "by", "the", "and", "or", "vs", "a", "an", "to", "in"})


def _metric_core(metric: str) -> frozenset[str]:
    """The semantic token-core of a finding/cluster metric label — drift's structural-
    vocabulary stripping (``drift._tokens``) plus connective-word removal, since a
    label ("sum of revenue") carries glue words a physical column name never does."""
    return frozenset(_semantic_tokens(metric) - _CONNECTIVES)


def governing_lens(
    metric: str,
    profiles: list[tuple[str, list[GovernedMetric]]],
    *,
    core: frozenset[str] | None = None,
) -> str | None:
    """The lens that governs ``metric``, or None (an ungoverned gap).

    Two-tier name match, embedder-free: a normalized-exact hit, else a semantic
    token-core overlap (the metric's core shares a token with a governed metric's
    core). The FIRST matching lens wins (profiles are passed name-sorted by the
    callers, so the binding is deterministic). Table overlap is never consulted.

    ``core`` overrides the core derived from ``metric`` — the gap-map passes a
    bucket's *accumulated* core (the union of merged labels) so a metric that merged
    in still binds on the tokens it contributed.
    """
    norm = normalize(metric)
    if core is None:
        core = _metric_core(metric)
    # Tier 1: normalized exact — the lens governs the metric under the same text.
    for lens, governed in profiles:
        if any(g.norm == norm for g in governed):
            return lens
    # Tier 2: semantic token-core overlap — "sum of revenue" ↔ "net invoiced revenue".
    if core:
        for lens, governed in profiles:
            if any(g.core & core for g in governed):
                return lens
    return None


# ── the gap-map (warehouse-level decomposition) ──────────────────────────────


class Gap(BaseModel):
    """One metric the org reasons about, decomposed: which evidence raised it, and
    which lens (if any) governs it. ``governed_by is None`` ⇒ a true coverage gap."""

    metric: str  # the metric's display label (the finding/cluster name)
    sources: list[str] = Field(default_factory=list)  # {"audit", "router"} — evidence
    governed_by: str | None = None  # the governing lens, or None for an UNGOVERNED gap
    blast_radius: int = 0  # audit run-count weight, when the audit raised it
    decline_count: int = 0  # router declines, when the router raised it
    examples: list[str] = Field(default_factory=list)  # up to 3 verbatim declines


class GapMap(BaseModel):
    """The connection's coverage decomposition: governed metrics (by which lens) vs
    ungoverned gaps, with counts. The unified worklist behind the three surfaces."""

    connection: str
    gaps: list[Gap]

    @property
    def governed(self) -> list[Gap]:
        return [g for g in self.gaps if g.governed_by is not None]

    @property
    def ungoverned(self) -> list[Gap]:
        return [g for g in self.gaps if g.governed_by is None]

    @property
    def governed_count(self) -> int:
        return len(self.governed)

    @property
    def ungoverned_count(self) -> int:
        return len(self.ungoverned)


@dataclass
class _GapAccumulator:
    """Mutable de-dupe bucket for one metric across the two evidence sources. Merges
    by semantic token-core *overlap* (so audit's "count of churn" and a router
    "churn" decline collapse into one gap), tracking the union of cores it absorbed."""

    metric: str
    core: frozenset[str]  # the union of merged-in cores — grows as metrics absorb
    sources: set[str] = field(default_factory=set)
    blast_radius: int = 0
    decline_count: int = 0
    examples: list[str] = field(default_factory=list)


def build_gap_map(
    connection: str,
    *,
    findings: list[DriftFinding],
    declines: list[UncoveredCluster],
    profiles: list[CoverageProfile],
) -> GapMap:
    """Bind router uncovered-metric clusters + audit ungoverned drift into one
    de-duped gap-map, each gap tagged with its evidence source(s) and its governing
    lens (or UNGOVERNED). Pure: no DB, no network — the DB wiring is in the endpoint.

    De-dupe is across the two sources by metric: two labels merge when their semantic
    token-cores overlap ("sum of revenue" ↔ a "revenue" cluster), else by normalized
    text. Each merged metric is then resolved to its governing lens by metric-
    governance match (never tables).
    """
    # Profiles passed name-sorted so governing_lens is deterministic on ties.
    governed_by_lens = sorted(
        ((p.lens, _governed_metrics(p)) for p in profiles), key=lambda x: x[0]
    )

    # Insertion-ordered buckets; a metric joins the first bucket whose accumulated
    # core it overlaps (single-link merge), else opens a new one. Coreless labels key
    # on normalized text. Audit is processed first so a drift name anchors the bucket.
    buckets: list[_GapAccumulator] = []
    coreless: dict[str, _GapAccumulator] = {}

    def _bucket(metric: str) -> _GapAccumulator:
        core = _metric_core(metric)
        if not core:
            key = normalize(metric)
            acc = coreless.get(key)
            if acc is None:
                acc = _GapAccumulator(metric=metric, core=frozenset())
                coreless[key] = acc
                buckets.append(acc)
            return acc
        for acc in buckets:
            if acc.core & core:
                acc.core |= core  # absorb — the bucket's reach grows
                return acc
        acc = _GapAccumulator(metric=metric, core=core)
        buckets.append(acc)
        return acc

    # Audit ungoverned drift — each finding is a metric computed several ways.
    for finding in findings:
        acc = _bucket(finding.metric_intent)
        acc.sources.add("audit")
        acc.blast_radius += finding.blast_radius

    # Router declines — uncovered-metric clusters (live surface misses).
    for cluster in declines:
        acc = _bucket(cluster.label)
        acc.sources.add("router")
        acc.decline_count += cluster.count
        for ex in cluster.examples:
            if ex not in acc.examples and len(acc.examples) < 3:
                acc.examples.append(ex)

    gaps = [
        Gap(
            metric=acc.metric,
            sources=sorted(acc.sources),
            # Resolve over the bucket's full accumulated core (the union of every
            # merged label), not just the anchor metric's — a "revenue" decline that
            # merged into a "sum of revenue" bucket must still bind to finance.
            governed_by=governing_lens(acc.metric, governed_by_lens, core=acc.core),
            blast_radius=acc.blast_radius,
            decline_count=acc.decline_count,
            examples=acc.examples,
        )
        for acc in buckets
    ]
    # Ungoverned gaps first (the worklist), then by weight, then name — stable.
    gaps.sort(
        key=lambda g: (
            g.governed_by is not None,
            -(g.blast_radius + g.decline_count),
            g.metric,
        )
    )
    return GapMap(connection=connection, gaps=gaps)


# ── the per-lens view (in-scope governed findings only) ──────────────────────


class InScopeFinding(BaseModel):
    """One audit/router finding a lens governs — the slice it is accountable to
    resolve canonically. Out-of-scope findings never appear here (by construction)."""

    metric: str
    source: str  # "audit" | "router"
    # The lens claims (governs) this finding ⇒ it is the accountable resolver.
    governed: bool = True
    blast_radius: int = 0
    decline_count: int = 0


class LensCoverage(BaseModel):
    """A lens's in-scope coverage slice: only the governed findings it must resolve."""

    lens: str
    in_scope: list[InScopeFinding]

    @property
    def in_scope_count(self) -> int:
        return len(self.in_scope)


def lens_in_scope_findings(
    profile: CoverageProfile,
    *,
    findings: list[DriftFinding],
    declines: list[UncoveredCluster],
) -> LensCoverage:
    """The findings WITHIN a lens's scope — only those whose metric the lens governs.

    Scope-binding is by metric governance (``governing_lens`` resolving to THIS
    lens), not table overlap. Out-of-scope drift is excluded by construction: a
    finance lens governing revenue/overdue never sees a churn or win-rate finding,
    even if their SQL touches a table the lens also reads. Each in-scope finding is
    flagged ``governed=True`` — the lens is the accountable canonical resolver.
    """
    governed = [(profile.lens, _governed_metrics(profile))]

    def _ours(metric: str) -> bool:
        return governing_lens(metric, governed) == profile.lens

    in_scope: list[InScopeFinding] = []
    for finding in findings:
        if _ours(finding.metric_intent):
            in_scope.append(
                InScopeFinding(
                    metric=finding.metric_intent,
                    source="audit",
                    blast_radius=finding.blast_radius,
                )
            )
    for cluster in declines:
        if _ours(cluster.label):
            in_scope.append(
                InScopeFinding(
                    metric=cluster.label,
                    source="router",
                    decline_count=cluster.count,
                )
            )
    in_scope.sort(key=lambda f: (-(f.blast_radius + f.decline_count), f.metric))
    return LensCoverage(lens=profile.lens, in_scope=in_scope)
