"""dbt import — one-shot scaffolding into the shared semantic layer.

Reads a dbt project's compiled artifacts (``manifest.json`` + ``semantic_manifest.json``)
and compiles them deterministically into dst-owned shared-layer files — dbt as a
starting point, dst-owned from the moment it lands (never re-synced).
"""

from __future__ import annotations

from services.dbt.artifacts import (
    DbtArtifactError,
    DbtArtifacts,
    DbtColumn,
    DbtDimension,
    DbtEntity,
    DbtMeasure,
    DbtMetric,
    DbtModel,
    DbtSemanticModel,
    load_artifacts,
    parse_artifacts,
)
from services.dbt.compile import (
    ImportResult,
    SkippedConstruct,
    import_shared_assets,
)
from services.dbt.report import (
    CompiledItem,
    CoverageReport,
    coverage_report,
    render_text,
)

__all__ = [
    "ImportResult",
    "CompiledItem",
    "CoverageReport",
    "DbtArtifactError",
    "DbtArtifacts",
    "DbtColumn",
    "DbtDimension",
    "DbtEntity",
    "DbtMeasure",
    "DbtMetric",
    "DbtModel",
    "DbtSemanticModel",
    "SkippedConstruct",
    "import_shared_assets",
    "coverage_report",
    "load_artifacts",
    "parse_artifacts",
    "render_text",
]
