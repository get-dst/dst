"""Definition-drift miner — the contradiction probe.

Distills warehouse query history (``QueryRecord``s) into a **Definition
Drift Report**: the same business metric computed N different ways across the
org, with the blast radius (run counts) and who/where each variant runs.

Pipeline (``mine_drift`` is the pure entry point — no DB, no network):

1. **Fingerprint** each statement with sqlglot: the literal-free canonical shape
   (reusing the distiller's ``normalize_sql_shape``), the primary aggregate
   (SUM/COUNT/AVG/MIN/MAX over a measure-ish expression), source tables, the
   literal-free filter shape, and the time grain (DATE_TRUNC unit). Statements
   without an aggregate, or that don't parse, are skipped — not guessed at.
2. **Cluster by metric intent.** The load-bearing choice: variants of one
   business metric usually hit *different* tables (``gold.fct_revenue_monthly``
   vs ``gold.sales_revenue_monthly`` vs ``gold.dashboard_revenue_old``) and
   differently-named columns (``net_revenue_eur`` vs ``revenue_eur``), so the
   cluster key cannot be the target. Instead, two fingerprints merge
   (union-find) when they share the **aggregate family** (SUM with SUM, COUNT
   with COUNT) *and* their **semantic token cores overlap**: measure-column +
   table names are tokenized, structural warehouse vocabulary is stripped
   (layer names like gold/stg, role prefixes like fct/dim/dashboard/old, grain
   words like monthly, units like eur, id-words), and a naive plural stem is
   applied — leaving ``{revenue}`` as the shared core of all three revenue
   variants while ``SUM(freight_cost_eur)`` stays apart.
3. **Variants = distinct shapes** within a cluster: literal-variants of one
   definition (``fiscal_year = 2025`` vs ``2024``) fold into a single variant;
   run counts, principals and source tools aggregate across the fold. Corollary
   (a documented limitation): a war that differs ONLY in a literal — a 120- vs
   180-day renewal window — folds into one variant too; pure literal drift is
   out of scope for a shape miner.
4. **Diff the definitions** across variants — measure expression (incl.
   DISTINCT vs not), source tables, filter shape, time grain. Any difference
   that could change the number ⇒ severity ``"conflict"``; equivalent
   definitions in different SQL text ⇒ ``"duplication"``.

An optional LLM names each finding (one call per finding, blast-radius order —
deterministic for ScriptedLLM); ``llm=None`` falls back to a token-derived name
("sum of revenue"). ``execute_variants`` optionally runs each variant against a
connector — **read-only, hard-guarded against writes, row-capped** — and
attaches the actual diverging numbers to the report.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from typing import Literal, NamedTuple

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from services.connectors.tagging import is_dst_query, query_context
from services.contracts.protocols import CacheableBlock, Connector, LLMProvider, Message
from services.contracts.query_history import QueryRecord
from services.evals.distill import normalize_sql_shape


class DriftVariant(BaseModel):
    """One distinct definition of the metric, with where and how often it runs."""

    statement: str  # representative statement (the most-run literal variant)
    source_tables: list[str]
    run_count: int
    principals: list[str] = Field(default_factory=list)
    source_tools: list[str] = Field(default_factory=list)
    distinguishing: str  # what this variant does differently from its siblings
    # Filled by execute_variants — the actual numbers this definition produces.
    observed_columns: list[str] | None = None
    observed_rows: list[list[object]] | None = None
    observed_error: str | None = None
    # Filled by services.probe.correctness — the medallion tier of this variant's
    # source tables ("gold" | "silver" | "bronze" | "unknown"). Drives which reading
    # is proposed as canon and how the dashboard colors it.
    tier: str | None = None


class CanonMatch(BaseModel):
    """How the audit's proposed canon relates to a declared org standard.

    Filled only when the org has *declared* a canonical definition (an org standard,
    e.g. compiled from dbt) for this metric. ``agrees`` = the proposed-canon reading
    matches the declared one; ``contradicts`` = a *different* observed reading matches
    the declared definition than the one the audit would crown (the sharpest signal —
    "you compute this several ways and the popular one isn't the one you declared");
    ``undetermined`` = a standard exists but its SQL can't be aligned to a variant. A
    finding with ``canon_match=None`` simply has no declared canon to measure against.
    """

    term: str  # the declared standard's term that this finding matched
    state: Literal["agrees", "contradicts", "undetermined"]


class DriftFinding(BaseModel):
    """One metric computed several ways — the unit of the Definition Drift Report."""

    metric_intent: str
    variants: list[DriftVariant]
    blast_radius: int  # total runs across all variants
    severity: Literal["conflict", "duplication"]
    # Filled by services.probe.correctness for conflicts — the index of the variant
    # proposed as the correct reading (skill-aware: medallion tier, not a vote) and a
    # one-line reason. None when correctness has not been annotated (the renderers then
    # fall back to the deterministic most-agreed/most-run rule).
    canon_index: int | None = None
    canon_rationale: str | None = None
    # Filled by services.probe.report.annotate_against_canon when the org has declared a
    # canonical definition for this metric — None = no declared canon.
    canon_match: CanonMatch | None = None
    # Filled by execute_variants: True = the definitions differ but every variant
    # produced identical results on today's data (a latent conflict, not a live one);
    # False = the numbers actually diverge. None = not executed or not comparable.
    values_agree: bool | None = None


# ── 1. fingerprints ───────────────────────────────────────────────────────────

_FAMILIES: dict[type[exp.Expression], str] = {
    exp.Sum: "SUM",
    exp.Count: "COUNT",
    exp.CountIf: "COUNT",  # not a Count subclass in sqlglot — BigQuery ratio metrics live on it
    exp.Avg: "AVG",
    exp.Min: "MIN",
    exp.Max: "MAX",
}

# Warehouse-structural vocabulary stripped from names before intent matching:
# none of these words carries the *business* identity of a metric.
_STRUCTURAL_TOKENS = frozenset(
    {
        # layer / schema names
        "gold", "silver", "bronze", "raw", "base", "staging", "stg", "src",
        "mart", "analytics", "public", "main", "dbo", "prod", "dev", "dwh",
        "warehouse", "lake",
        # table-role prefixes/suffixes
        "fct", "fact", "dim", "agg", "rpt", "report", "mv", "vw", "view",
        "tbl", "tmp", "temp", "old", "new", "legacy", "final", "current",
        "snapshot", "dashboard",
        # grain / time words
        "daily", "weekly", "monthly", "quarterly", "yearly", "annual",
        "day", "week", "month", "quarter", "year", "date", "dt", "ts",
        "timestamp",
        # units / generic measure words
        "eur", "usd", "gbp", "amt", "total", "sum", "count", "cnt", "avg",
        "pct", "ratio", "rate", "num",
        # identifier words
        "id", "key", "pk", "sk", "nbr", "no", "code",
    }
)  # fmt: skip

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stem(token: str) -> str:
    """Naive plural stem so ``fct_invoices`` matches ``invoice_id``."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _tokens(*names: str) -> frozenset[str]:
    """The semantic token core of a set of column/table/alias names."""
    raw = {
        _stem(tok)
        for name in names
        for tok in _TOKEN_RE.findall(name.lower())
        if not tok.isdigit() and len(tok) > 1  # 1-char aliases (n, c) carry no identity
    }
    core = raw - _STRUCTURAL_TOKENS
    return frozenset(core or raw)  # all-structural names keep their raw tokens


class _Fingerprint(NamedTuple):
    """Everything the miner needs to know about one observed statement."""

    record: QueryRecord
    shape: str  # literal-free canonical SQL — the variant key
    family: str  # aggregate family: SUM/COUNT/AVG/MIN/MAX
    measure: str  # canonical aggregate expression, e.g. "COUNT(DISTINCT invoice_id)"
    tables: tuple[str, ...]
    filters: str | None  # literal-free WHERE shape
    grain: str | None  # DATE_TRUNC unit, when present
    tokens: frozenset[str]
    report: bool  # 3+ aggregates = a report consuming metrics, not defining one
    groupby: frozenset[str]  # GROUP BY column tokens — a report's breakdown dimension
    aliases: frozenset[str]  # projection alias tokens — what the author calls the outputs


def _strip_literals(node: exp.Expression) -> exp.Expression:
    return exp.Placeholder() if isinstance(node, exp.Literal) else node


def _unwrap_star(tree: exp.Expression) -> exp.Select | None:
    """The statement's semantic core: peel bare star wrappers off the outermost SELECT.

    ``SELECT * FROM (<inner>) AS _q LIMIT n`` — the BI-tool/row-cap idiom, including
    dst's own execute() wrapping — carries no intent of its own. Fingerprinting
    the core makes a wrapped and a raw run of the same statement ONE variant (on a
    live warehouse over half the history arrived wrapped, double-counting every
    definition), and lets the aggregate scan see through the wrapper. A wrapper that
    adds its own WHERE/GROUP BY/HAVING/QUALIFY is semantic and is NOT peeled.
    """
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    while select is not None:
        projections: list[exp.Expression] = select.expressions
        if not projections or not all(p.is_star for p in projections):
            return select
        if any(select.args.get(k) for k in ("where", "group", "having", "qualify")):
            return select
        source = select.args.get("from_") or select.args.get("from")  # key renamed across sqlglot
        inner = (
            source.this if source is not None and isinstance(source.this, exp.Subquery) else None
        )
        if inner is None:
            return select
        select = inner.find(exp.Select)
    return None


def _primary_aggregate(select: exp.Select) -> exp.Expression | None:
    """The first SUM/COUNT/AVG/MIN/MAX in the core SELECT list."""
    projections: list[exp.Expression] = select.expressions
    for projection in projections:
        for node in projection.find_all(*_FAMILIES):
            return node
    return None


def _source_tables(tree: exp.Expression) -> tuple[str, ...]:
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        if not tbl.name or (tbl.name.lower() in cte_names and not tbl.db):
            continue
        out.add(".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p).lower())
    return tuple(sorted(out))


def _filter_shape(tree: exp.Expression, dialect: str) -> str | None:
    shapes = sorted(
        w.this.transform(_strip_literals).sql(dialect=dialect) for w in tree.find_all(exp.Where)
    )
    return " AND ".join(shapes) if shapes else None


def _time_grain(tree: exp.Expression) -> str | None:
    for node in tree.find_all(exp.DateTrunc, exp.TimestampTrunc):
        unit = node.args.get("unit")
        if isinstance(unit, exp.Expression):
            return unit.name.lower()
    return None


# 3+ aggregate expressions = an exploratory report consuming several metrics at
# once, not a competing definition of one (the same boundary as wide joins below).
_MAX_DEFINITION_AGGS = 2


def _fingerprint(record: QueryRecord, dialect: str) -> _Fingerprint | None:
    """Fingerprint one history record; None = not a metric statement (skipped)."""
    try:
        tree = sqlglot.parse_one(record.statement, read=dialect)
    except Exception:
        return None
    if not isinstance(tree, exp.Expression):  # parse_one is typed to the wider Expr
        return None
    core = _unwrap_star(tree)
    if core is None:
        return None
    shape = normalize_sql_shape(core.sql(dialect=dialect), dialect)
    if shape is None:
        return None
    agg = _primary_aggregate(core)
    if agg is None:
        return None  # no aggregate — not a metric intent
    family = next(name for cls, name in _FAMILIES.items() if isinstance(agg, cls))
    tables = _source_tables(core)
    projections: list[exp.Expression] = core.expressions
    agg_count = sum(1 for p in projections for _ in p.find_all(*_FAMILIES))
    aliases = [p.alias for p in projections if isinstance(p, exp.Alias) and p.alias]
    group = core.args.get("group")
    group_columns = [c.name for c in group.find_all(exp.Column)] if group is not None else []
    # The metric's identity: what it measures, and what the author calls it. A bare
    # COUNT(*) measures nothing by name, so its identity falls back to how it is
    # sliced and filtered; only a totally mute statement falls back to its table.
    # Tables contribute LEAF names only: catalog/dataset tokens (a BigQuery project
    # name) appear in every FQN of a warehouse and would glue every same-family
    # fingerprint into one mega-cluster.
    measure_columns = [c.name for c in agg.find_all(exp.Column)]
    primary_alias = next(
        (
            p.alias
            for p in projections
            if isinstance(p, exp.Alias) and any(n is agg for n in p.find_all(*_FAMILIES))
        ),
        None,
    )
    identity = _tokens(*measure_columns, *([primary_alias] if primary_alias else []))
    if not identity:
        where_columns = [c.name for w in core.find_all(exp.Where) for c in w.find_all(exp.Column)]
        identity = _tokens(*group_columns, *where_columns)
    if not identity:
        identity = _tokens(*(t.rsplit(".", 1)[-1] for t in tables))
    return _Fingerprint(
        record=record,
        shape=shape,
        family=family,
        measure=agg.transform(_strip_literals).sql(dialect=dialect),
        tables=tables,
        filters=_filter_shape(core, dialect),
        grain=_time_grain(core),
        tokens=identity,
        report=agg_count > _MAX_DEFINITION_AGGS,
        groupby=_tokens(*group_columns) if group_columns else frozenset(),
        aliases=_tokens(*aliases) if aliases else frozenset(),
    )


# ── 2. intent clustering (union-find on family + token overlap) ──────────────


# 3+ source tables = a report CONSUMING metrics, not a definition of one. Such a
# query overlaps tokens with every metric it touches, and under union-find one of
# them transitively glues unrelated clusters into a single mega-finding.
_MAX_DEFINITION_TABLES = 2


def _cluster_by_intent(prints: list[_Fingerprint]) -> list[list[_Fingerprint]]:
    parent = list(range(len(prints)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(prints)):
        for j in range(i + 1, len(prints)):
            a, b = prints[i], prints[j]
            if a.family != b.family:
                continue
            if a.report or b.report:
                # Reports never glue to metric statements, and fold with each other
                # only when they are recognizably the SAME report: same tables, same
                # breakdown dimension, overlapping output names.
                if not (a.report and b.report):
                    continue
                if a.tables != b.tables or a.groupby != b.groupby:
                    continue
                if not (a.aliases & b.aliases):
                    continue
            else:
                if not (a.tokens & b.tokens):
                    continue
                wide = (
                    len(a.tables) > _MAX_DEFINITION_TABLES or len(b.tables) > _MAX_DEFINITION_TABLES
                )
                if wide and a.tables != b.tables:
                    continue  # wide consumers may only fold with their own table set
            parent[find(i)] = find(j)

    groups: dict[int, list[_Fingerprint]] = {}
    for i, fp in enumerate(prints):
        groups.setdefault(find(i), []).append(fp)
    return list(groups.values())


# ── 3/4. variants + definition diff → finding ────────────────────────────────


def _distinguishing(
    rep: _Fingerprint, *, measure: bool, tables: bool, filters: bool, grain: bool
) -> str:
    """Describe what this variant does differently — only the facets that vary."""
    parts: list[str] = []
    if measure:
        parts.append(rep.measure)
    if tables:
        parts.append("over " + (", ".join(rep.tables) or "(no table)"))
    if filters:
        parts.append(f"filters: {rep.filters}" if rep.filters else "no filters")
    if grain:
        parts.append(f"grain: {rep.grain}" if rep.grain else "no explicit grain")
    return "; ".join(parts) or "equivalent definition, different SQL text"


def _fallback_name(cluster: list[_Fingerprint]) -> str:
    """Deterministic metric name from the shared token core, e.g. "sum of revenue"."""
    shared = frozenset.intersection(*(fp.tokens for fp in cluster))
    if shared:
        label = " ".join(sorted(shared))
    else:
        counts = Counter(tok for fp in cluster for tok in fp.tokens)
        label = min(counts, key=lambda t: (-counts[t], t)) if counts else "unnamed metric"
    return f"{cluster[0].family.lower()} of {label}"


def _build_finding(cluster: list[_Fingerprint], min_cluster: int) -> DriftFinding | None:
    by_shape: dict[str, list[_Fingerprint]] = {}
    for fp in cluster:
        by_shape.setdefault(fp.shape, []).append(fp)
    if len(by_shape) < min_cluster:
        return None  # one definition (however popular) is not drift

    reps = {
        shape: min(group, key=lambda f: (-f.record.run_count, f.record.statement))
        for shape, group in by_shape.items()
    }
    vary_measure = len({fp.measure for fp in reps.values()}) > 1
    vary_tables = len({fp.tables for fp in reps.values()}) > 1
    vary_filters = len({fp.filters for fp in reps.values()}) > 1
    vary_grain = len({fp.grain for fp in reps.values()}) > 1
    severity: Literal["conflict", "duplication"] = (
        "conflict" if vary_measure or vary_tables or vary_filters or vary_grain else "duplication"
    )

    variants = [
        DriftVariant(
            statement=reps[shape].record.statement,
            source_tables=list(reps[shape].tables),
            run_count=sum(fp.record.run_count for fp in group),
            principals=sorted({fp.record.principal for fp in group if fp.record.principal}),
            source_tools=sorted({fp.record.source_tool for fp in group if fp.record.source_tool}),
            distinguishing=_distinguishing(
                reps[shape],
                measure=vary_measure,
                tables=vary_tables,
                filters=vary_filters,
                grain=vary_grain,
            ),
        )
        for shape, group in by_shape.items()
    ]
    variants.sort(key=lambda v: (-v.run_count, v.statement))
    return DriftFinding(
        metric_intent=_fallback_name(cluster),
        variants=variants,
        blast_radius=sum(v.run_count for v in variants),
        severity=severity,
    )


# ── optional LLM naming (behind LLMProvider; ScriptedLLM in tests) ────────────

_NAME_SYSTEM = (
    "You name business metrics mined from a data warehouse's query history. Given "
    "several SQL variants that all compute one recurring metric intent, give that "
    "metric a short business name a data team would recognize. Respond with strict "
    'JSON: {"name": "<2-4 word metric name>"}.'
)

_MAX_SAMPLE_VARIANTS = 5


def _llm_name(
    llm: LLMProvider, finding: DriftFinding, model: str, avoid: frozenset[str] = frozenset()
) -> str:
    sample = "\n".join(f"- {v.statement}" for v in finding.variants[:_MAX_SAMPLE_VARIANTS])
    prompt = f"Working name: {finding.metric_intent}\nVariants:\n{sample}"
    if avoid:
        prompt += "\nAlready used in this report (pick something distinct): " + "; ".join(
            sorted(avoid)
        )
    res = llm.complete(
        system=[CacheableBlock(_NAME_SYSTEM)],
        messages=[Message("user", prompt)],
        model=model,
        temperature=0.0,
        # Reasoning-style fast models (deepseek-v4-flash, the default fast tier) spend
        # output budget thinking before emitting content — 100 starved the response to
        # an empty string and every production finding kept its fallback name.
        max_tokens=1000,
    )
    try:
        parsed = json.loads(res.text.strip())
    except json.JSONDecodeError:
        return finding.metric_intent  # bad JSON → keep the deterministic name
    if not isinstance(parsed, dict):
        return finding.metric_intent
    name = str(parsed.get("name") or "").strip()
    return name or finding.metric_intent


def _name_findings(
    llm: LLMProvider, findings: list[DriftFinding], model: str
) -> list[DriftFinding]:
    """Name each finding, retrying once on a fallback or a duplicate — two findings
    called "Quote Win Rate" in one report is worse than a deterministic name."""
    named: list[DriftFinding] = []
    taken: set[str] = set()
    for f in findings:
        name = _llm_name(llm, f, model)
        if name == f.metric_intent or name.lower() in taken:
            name = _llm_name(llm, f, model, avoid=frozenset(taken | {name.lower()}))
        base, n = name, 2
        while name.lower() in taken:
            name, n = f"{base} ({n})", n + 1
        taken.add(name.lower())
        named.append(f.model_copy(update={"metric_intent": name}))
    return named


# ── the miner ─────────────────────────────────────────────────────────────────


def mine_drift(
    records: list[QueryRecord],
    llm: LLMProvider | None = None,
    *,
    min_cluster: int = 2,
    dialect: str = "snowflake",
    model: str = "claude-haiku-4-5",
) -> list[DriftFinding]:
    """Mine query history for definition drift — the pure entry point.

    A finding needs at least ``min_cluster`` *distinct definitions* (variants) of
    one metric intent: singleton intents and literal-only repeats emit nothing.
    Findings are ordered by blast radius (total runs) descending; the optional
    ``llm`` names each finding in that order (deterministic for ScriptedLLM),
    ``llm=None`` keeps the token-derived fallback names.
    """
    # dst's own traffic never mines as organic practice: every statement a
    # connector executes carries the tag (services/connectors/tagging.py), so
    # governed serves and this audit's own variant re-runs are excluded here
    # mechanically, before fingerprinting.
    organic = [r for r in records if not is_dst_query(r.statement)]
    prints = [fp for r in organic if (fp := _fingerprint(r, dialect)) is not None]
    findings = [
        finding
        for cluster in _cluster_by_intent(prints)
        if (finding := _build_finding(cluster, min_cluster)) is not None
    ]
    findings.sort(key=lambda f: (-f.blast_radius, f.metric_intent))
    if llm is not None:
        findings = _name_findings(llm, findings, model)
    return findings


# ── optional execution: attach the actual diverging numbers ──────────────────

# Mirror of sql_guard's forbidden-node check — the probe never carries a
# lens/semantic model, so it gets its own SELECT-only gate with the same teeth.
_FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
        "TruncateTable", "Command", "Set", "Pragma", "Copy", "Use",
    )
    if hasattr(exp, name)
)  # fmt: skip


def _refuse_reason(statement: str, dialect: str) -> str | None:
    """None when the statement is a single pure SELECT; otherwise why it must not run."""
    try:
        statements = [s for s in sqlglot.parse(statement, read=dialect) if s is not None]
    except Exception as exc:
        return f"parse error: {exc}"
    if len(statements) != 1:
        return "multiple statements"
    stmt = statements[0]
    for node in stmt.find_all(*_FORBIDDEN_NODES):
        return f"non-SELECT operation ({type(node).__name__})"
    if not isinstance(stmt, exp.Select | exp.Union | exp.Subquery | exp.With):
        return "not a SELECT"
    return None


def execute_variants(
    connector: Connector, finding: DriftFinding, *, row_limit: int = 5
) -> DriftFinding:
    """Run each variant read-only and attach the numbers it actually produces.

    Hard guards: every statement is re-checked to be a single pure SELECT before
    execution (writes are *never* issued — a refused variant gets an
    ``observed_error`` instead), and every call goes through
    ``connector.execute(read_only=True, row_limit=row_limit)``. Returns a new
    finding; the input is untouched. Execution errors attach per-variant rather
    than failing the report.
    """
    variants: list[DriftVariant] = []
    for variant in finding.variants:
        reason = _refuse_reason(variant.statement, connector.kind)
        if reason is not None:
            variants.append(variant.model_copy(update={"observed_error": f"not run: {reason}"}))
            continue
        try:
            with query_context(purpose="audit"):
                result = connector.execute(variant.statement, read_only=True, row_limit=row_limit)
        except Exception as exc:
            variants.append(variant.model_copy(update={"observed_error": str(exc)}))
            continue
        variants.append(
            variant.model_copy(
                update={"observed_columns": result.columns, "observed_rows": result.rows}
            )
        )
    return finding.model_copy(
        update={"variants": variants, "values_agree": _values_agree(variants)}
    )


def _values_agree(variants: list[DriftVariant]) -> bool | None:
    """Whether every executed variant produced the same NUMBERS (order-insensitive).

    "Conflict" describes the *definitions*; this describes today's *numbers* — a
    conflict whose variants all agree is latent, not live, and the report should
    say so instead of crying wolf. Only numeric cells are compared: variants are
    different SQL by construction, so whole-row equality would almost never fire —
    two payout formulas returning identical totals through different dimension
    columns must still read as agreement.
    """
    if len(variants) < 2:
        return None
    seen: set[tuple[str, ...]] = set()
    for v in variants:
        if v.observed_error is not None or v.observed_rows is None:
            return None
        numbers = sorted(
            f"{float(c):.6g}"
            for row in v.observed_rows
            for c in row
            if isinstance(c, int | float | Decimal) and not isinstance(c, bool)
        )
        if not numbers:
            return None  # nothing numeric to compare
        seen.add(tuple(numbers))
    return len(seen) == 1
