"""The probe artifact — the warehouse's full physical truth, kept in the project.

`dst probe` writes `profiles/<conn>.probe.json`: the catalog + sampling
passes (partitions, row counts, freshness, null rates, value dictionaries)
crossed with the semantic layer — each table annotated with the entities that
read it, so the file reads as *the warehouse under the layer*, not a bare
catalog dump. Machine-owned and rewritten whole on every probe; refresh is the
command on a cron, nightly is plenty.

The drift baseline (`profiles/<conn>.json`) is this file's deliberate opposite
and stays: it whitelists schema fields so `--accept` history stays quiet and no
literal reaches git through it. The probe artifact exists BECAUSE the value
literals must reach the project: a pipeline that has never seen `country` hold
'FI' writes `WHERE country = 'Finland'` and grades the zero verified. Sampled
literals in git is the point (lens-as-repo) — which is also why a column you do
not want in the repo belongs in `exclude_columns`, where the sampler never reads
it. The two shapes are disjoint on purpose (`probed_at` vs `recorded_at`), so
neither reader ever accepts the other's file.

Apply ingests the artifact into the store the runtime already reads
(`apply_profiles`); serving needs no new seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from services.contracts.profile import SampleWindow, TableProfile, is_temporal_type
from services.contracts.shared_semantic import SharedEntity
from services.project.warehouse_drift import BASELINE_DIR, connection_slug, same_table

PROBE_SUFFIX = ".probe.json"


class ProbeArtifact(BaseModel):
    connection: str
    probed_at: datetime
    tables: list[TableProfile] = Field(default_factory=list)
    # table -> entity names that read it (drift's matching rule). Tables absent
    # from the map are the unmapped ones — that gap is authoring signal too.
    entities: dict[str, list[str]] = Field(default_factory=dict)


def probe_path(root: Path, connection: str) -> Path:
    return root / BASELINE_DIR / f"{connection_slug(connection)}{PROBE_SUFFIX}"


def is_probe_path(path: str) -> bool:
    """Whether a pushed project path is a probe artifact (apply's selection rule)."""
    return path.startswith(f"{BASELINE_DIR}/") and path.endswith(PROBE_SUFFIX)


def entity_map(profiles: list[TableProfile], entities: list[SharedEntity]) -> dict[str, list[str]]:
    """table -> sorted entity names reading it; a table nothing reads gets no key."""
    out: dict[str, list[str]] = {}
    for profile in profiles:
        names = sorted(e.name for e in entities if same_table(e.source.table, profile.table))
        if names:
            out[profile.table] = names
    return out


def write_probe(
    root: Path, connection: str, profiles: list[TableProfile], entities: list[SharedEntity]
) -> tuple[Path, ProbeArtifact]:
    """Annotate, sort, write. Sorted so an unchanged warehouse diffs to timestamps only.

    Returns the artifact too, so a caller can summarize exactly what it wrote
    instead of re-deriving it from its own inputs.
    """
    tables = sorted(profiles, key=lambda p: p.table)
    artifact = ProbeArtifact(
        connection=connection,
        probed_at=datetime.now(UTC),
        tables=tables,
        entities=entity_map(tables, entities),
    )
    path = probe_path(root, connection)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path, artifact


def read_probe(root: Path, connection: str) -> ProbeArtifact | None:
    """The recorded artifact, or None — a corrupt file costs a re-probe, never a command."""
    path = probe_path(root, connection)
    if not path.exists():
        return None
    try:
        artifact = ProbeArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    # Two connection names can slug to one filename; the name inside decides.
    return artifact if artifact.connection == connection else None


def parse_probe(content: str, path: str) -> ProbeArtifact:
    """An apply-pushed artifact, or ValueError naming the file that failed."""
    try:
        return ProbeArtifact.model_validate_json(content)
    except ValueError as exc:
        first = str(exc).splitlines()[0]
        raise ValueError(f"{path}: not a probe artifact ({first})") from None


# ── recent-window sampling ───────────────────────────────────────────────────


def sample_windows(
    profiles: list[TableProfile], entities: list[SharedEntity], connection: str, n: int
) -> dict[str, SampleWindow]:
    """table -> the slice its sampling pass reads: the latest ``n`` partitions
    of a time-partitioned table, else the last ``n`` days of the entity's
    declared ``default_time_field``, else nothing (whole table, as today).
    ``n == 0`` asks for no window at all. BigQuery's ingestion-time
    partitioning has no column to anchor on and is recorded as such."""
    if n <= 0:
        return {}
    time_fields = {
        e.source.table: e.default_time_field
        for e in entities
        if e.source.connection == connection and e.source.table and e.default_time_field
    }
    out: dict[str, SampleWindow] = {}
    for profile in profiles:
        by_name = {c.name: c for c in profile.columns}
        part = profile.partitioning
        if part is not None and part.column and part.kind in (None, "time"):
            column = by_name.get(part.column)
            if column is not None and is_temporal_type(column.type):
                out[profile.table] = SampleWindow(kind="partitions", n=n, column=part.column)
                continue
        if part is not None and part.kind == "ingestion":
            out[profile.table] = SampleWindow(
                kind="none", n=0, reason="ingestion-time partitioning has no column to window on"
            )
            continue
        field = next(
            (f for t, f in time_fields.items() if same_table(t, profile.table) and f in by_name),
            None,
        )
        if field is not None and is_temporal_type(by_name[field].type):
            out[profile.table] = SampleWindow(kind="days", n=n, column=field)
    return out


def carry_complete_dictionaries(
    profiles: list[TableProfile], prior: ProbeArtifact | None
) -> list[TableProfile]:
    """A refresh read through a window keeps every dictionary the stored
    artifact had proved complete: the union, dated to the whole-table read
    that proved it. A window must never silently turn a complete enum into a
    partial one — and a value the window did see is still a value."""
    if prior is None:
        return profiles
    before = {p.table: p for p in prior.tables}
    out: list[TableProfile] = []
    for profile in profiles:
        old = before.get(profile.table)
        windowed = profile.sample_window is not None and profile.sample_window.kind != "none"
        if old is None or not windowed:
            out.append(profile)
            continue
        kept = {c.name: c for c in old.columns if c.values_complete and c.top_values}
        columns = []
        for column in profile.columns:
            previous = kept.get(column.name)
            if previous is None or column.values_complete:
                columns.append(column)
                continue
            seen = set(previous.top_values or [])
            union = list(previous.top_values or []) + [
                v for v in (column.top_values or []) if v not in seen
            ]
            columns.append(
                column.model_copy(
                    update={
                        "top_values": union,
                        "values_complete": True,
                        "values_complete_at": previous.values_complete_at or old.profiled_at,
                        "distinct_count": max(column.distinct_count or 0, len(union)),
                        "distinct_is_exact": previous.distinct_is_exact and len(union) == len(seen),
                        "is_low_cardinality": previous.is_low_cardinality,
                    }
                )
            )
        out.append(profile.model_copy(update={"columns": columns}))
    return out


# ── profile age at push time ─────────────────────────────────────────────────


def newest_profiled_at(files: Mapping[str, str]) -> dict[str, datetime]:
    """connection -> the newest ``profiled_at`` among the push's probe artifacts."""
    out: dict[str, datetime] = {}
    for path in sorted(files):
        if not is_probe_path(path):
            continue
        try:
            artifact = parse_probe(files[path], path)
        except ValueError:
            continue
        stamps = [_aware(p.profiled_at) for p in artifact.tables]
        if stamps:
            out[artifact.connection] = max(stamps)
    return out


def probe_age_warnings(
    files: Mapping[str, str], max_age: timedelta | None, *, now: datetime | None = None
) -> list[str]:
    """One warning per pushed artifact older than ``max_age`` — plan and apply
    both print it; apply still lands the artifact."""
    if max_age is None:
        return []
    moment = now or datetime.now(UTC)
    out: list[str] = []
    for connection, newest in sorted(newest_profiled_at(files).items()):
        age = moment - newest
        if age > max_age:
            out.append(
                f"profiles/{connection_slug(connection)}{PROBE_SUFFIX}: last probed "
                f"{age.days} days ago (max {_days(max_age)}) — run `dst probe` and commit "
                "the result"
            )
    return out


def _days(delta: timedelta) -> str:
    return f"{round(delta.total_seconds() / 86_400, 1):g}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
