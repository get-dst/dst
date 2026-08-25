"""Coverage matrix + honest "dbt doctor" report.

Turns a :class:`~services.dbt.compile.ImportResult` into a summary a data engineer reads
on every sync: exactly what compiled, what didn't, and why. The contract is **no silent
gaps** — every construct in ``result.skipped`` is surfaced as a "not synced: <reason>"
line. The compiler already refuses to drop constructs silently; this module makes that
visible.

Pure functions over the already-compiled artifacts; no I/O, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.dbt.artifacts import DbtArtifacts
from services.dbt.compile import ImportResult, SkippedConstruct


@dataclass(frozen=True)
class CompiledItem:
    """A dbt construct that compiled cleanly into the semantic model."""

    kind: str  # "semantic_model" | "measure" | "metric" | "dimension"
    name: str


@dataclass
class CoverageReport:
    """An honest support summary for one dbt → SemanticModel compile.

    Counts and item lists are derived purely from the artifacts + compile result; the
    skipped list mirrors ``result.skipped`` 1:1 so nothing is dropped silently.
    """

    project: str
    # counts
    semantic_models_total: int
    semantic_models_compiled: int
    measures_total: int
    measures_compiled: int
    measures_skipped: int
    metrics_total: int
    definitions_compiled: int
    metrics_skipped: int
    dimensions_compiled: int
    # item lists
    compiled: list[CompiledItem] = field(default_factory=list)
    skipped: list[SkippedConstruct] = field(default_factory=list)
    # top-level health
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def skipped_total(self) -> int:
        return len(self.skipped)


def coverage_report(artifacts: DbtArtifacts, result: ImportResult) -> CoverageReport:
    """Summarize what compiled and what didn't — a pure function over the compile result.

    ``ok`` is true only when nothing critical was dropped: every semantic model must have
    produced an entity *with an entity/primary key* (a grain we can actually query).
    Unsupported measures and definition-only metrics are non-fatal ``warnings``; a
    semantic model that produced no queryable entity is a fatal ``error``.
    """
    # Errors (fatal) vs warnings (non-fatal) come straight from the skip kinds, so the two
    # lists together account for every skipped construct — no silent gaps.
    errors: list[str] = []
    warnings: list[str] = []
    for s in result.skipped:
        line = f"{s.kind} '{s.name}': {s.reason}"
        if s.kind == "semantic_model":
            errors.append(line)
        else:
            warnings.append(line)

    # A compiled semantic model must have produced a queryable grain (a primary key).
    entities_with_grain = [e for e in result.entities if e.primary_key]
    for e in result.entities:
        if not e.primary_key:
            errors.append(
                f"semantic_model '{e.name}': compiled without a primary-key entity (no grain)"
            )

    compiled: list[CompiledItem] = []
    dimensions_compiled = 0
    for e in result.entities:
        compiled.append(CompiledItem("semantic_model", e.name))
        for m in e.metrics:
            compiled.append(CompiledItem("metric", m.name))
        for d in e.dimensions:
            compiled.append(CompiledItem("dimension", d.name))
            dimensions_compiled += 1
    for defn in result.definitions:
        compiled.append(CompiledItem("definition", defn.term))

    # Measure coverage is counted from the dbt source of truth, not from entity.metrics —
    # the compiler also surfaces simple dbt metrics (e.g. "revenue") as business-named
    # metrics on the entity, which would otherwise double-count the backing measures.
    measures_total = sum(len(sm.measures) for sm in artifacts.semantic_models)
    measures_skipped = sum(1 for s in result.skipped if s.kind == "measure")
    metrics_skipped = sum(1 for s in result.skipped if s.kind == "metric")
    measures_compiled = measures_total - measures_skipped

    ok = not errors and len(entities_with_grain) == len(artifacts.semantic_models)

    return CoverageReport(
        project=artifacts.project,
        semantic_models_total=len(artifacts.semantic_models),
        semantic_models_compiled=len(entities_with_grain),
        measures_total=measures_total,
        measures_compiled=measures_compiled,
        measures_skipped=measures_skipped,
        metrics_total=len(artifacts.metrics),
        definitions_compiled=len(result.definitions),
        metrics_skipped=metrics_skipped,
        dimensions_compiled=dimensions_compiled,
        compiled=compiled,
        skipped=list(result.skipped),
        ok=ok,
        warnings=warnings,
        errors=errors,
    )


def render_text(report: CoverageReport) -> str:
    """A concise, human-readable doctor report for an engineer to read on sync.

    Every skipped construct appears as a ``not synced: <reason>`` line — no silent gaps.
    """
    status = "OK" if report.ok else "ATTENTION"
    lines = [
        f"dbt sync — {report.project} [{status}]",
        (
            f"  semantic models: {report.semantic_models_compiled}/"
            f"{report.semantic_models_total} compiled"
        ),
        (
            f"  measures: {report.measures_compiled}/{report.measures_total} compiled "
            f"→ metrics ({report.measures_skipped} skipped)"
        ),
        (
            f"  metrics: {report.definitions_compiled}/{report.metrics_total} → definitions "
            f"({report.metrics_skipped} kept as prose / unavailable)"
        ),
        f"  dimensions: {report.dimensions_compiled} compiled",
    ]

    if report.errors:
        lines.append("  errors (not synced):")
        lines.extend(f"    - {e}" for e in report.errors)
    if report.warnings:
        lines.append("  warnings (not synced):")
        lines.extend(f"    - {w}" for w in report.warnings)

    # Explicitly enumerate every skipped construct so nothing is implicit.
    if report.skipped:
        lines.append("  skipped constructs:")
        for s in report.skipped:
            lines.append(f"    - {s.kind} '{s.name}': not synced: {s.reason}")
    else:
        lines.append("  no constructs skipped — full coverage.")

    return "\n".join(lines)
