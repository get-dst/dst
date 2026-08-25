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

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from services.contracts.profile import TableProfile
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
