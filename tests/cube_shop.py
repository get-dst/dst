"""The cube_shop fixture: a small entity-YAML project (tests/fixtures/cube_shop/
project), the Cube model ``dst migrate-model`` writes from it (its cube/), and
the /v1/meta a real Cube Core served over that model (extended): meta.json as
plan reads it (a caller in the ``ops`` exception group), meta_public.json as a
caller in no group."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from services.contracts.cube_meta import MetaDoc

FIXTURE = Path(__file__).parent / "fixtures" / "cube_shop"
PROJECT = FIXTURE / "project"


@cache
def _meta_payload(name: str = "meta.json") -> str:
    return (FIXTURE / name).read_text(encoding="utf-8")


def meta_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_meta_payload())
    return payload


def public_meta_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_meta_payload("meta_public.json"))
    return payload


def meta() -> MetaDoc:
    return MetaDoc.parse(meta_payload())


def project_files(duckdb_path: Path | None = None) -> dict[str, str]:
    """The project as `dst plan`/`apply` push it (lenses/, semantic/,
    profiles/, dst.yaml, cube/overlay.yaml); ``duckdb_path`` repoints the
    warehouse connection at a generated file."""
    files = {
        p.relative_to(PROJECT).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(PROJECT.rglob("*"))
        if p.is_file()
        and p.relative_to(PROJECT).as_posix().startswith(("lenses/", "semantic/", "profiles/"))
    }
    files["dst.yaml"] = (PROJECT / "dst.yaml").read_text(encoding="utf-8")
    files["cube/overlay.yaml"] = (PROJECT / "cube" / "overlay.yaml").read_text(encoding="utf-8")
    if duckdb_path is not None:
        files["dst.yaml"] = files["dst.yaml"].replace("path: shop.duckdb", f"path: {duckdb_path}")
    return files
