"""Certified answers on an install that cannot match them say so at plan and apply.

The failure: no embedder was configured on an install or its staging copy, so
certified matching never ran; every served answer carried a line saying so and
nothing failed. Plan and apply now name the gap and its fix on any lens that
declares certified answers, and say when the serve bands were never measured
for the configured embedder (a model outside the presets serves on defaults).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.app import app
from services.config import ProviderConfig, settings
from services.llm import registry
from services.project import apply as apply_engine
from tests.test_certified_status import _Q, _SQL, _files, needs_db, org  # noqa: F401

client = TestClient(app)


def _installed(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    monkeypatch.setattr(registry, "find_spec", lambda name: object() if name in modules else None)


def test_the_gap_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch)
    monkeypatch.setattr(settings, "providers", {})
    assert "dst-core[local-embed]" in (registry.embedder_gap() or "")
    # Declared in dst.yaml, extra missing on this install: the provider is named.
    monkeypatch.setattr(settings, "providers", {"embed": ProviderConfig(type="local")})
    gap = registry.embedder_gap() or ""
    assert "provider 'embed' declares embeddings" in gap and "local-embed" in gap
    _installed(monkeypatch, "fastembed")
    assert registry.embedder_gap() is None


def test_plan_line_degrades_without_an_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch)
    monkeypatch.setattr(settings, "providers", {})
    line = apply_engine.certified_matching_warning(3) or ""
    assert line.startswith("DEGRADED: 3 certified answer(s) declared")
    assert "certified matching will not run" in line
    assert apply_engine.certified_matching_warning(0) is None


def test_bands_are_keyed_per_model_and_others_fall_back_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.runtime.assembly import certified_bands, uncalibrated_bands_note

    _installed(monkeypatch, "fastembed")
    monkeypatch.setattr(settings, "providers", {"embed": ProviderConfig(type="local")})
    assert uncalibrated_bands_note() is None and certified_bands().exact == 0.93
    assert apply_engine.certified_matching_warning(2) is None
    larger = ProviderConfig(type="local", embedding_model="BAAI/bge-large-en-v1.5")
    monkeypatch.setattr(settings, "providers", {"embed": larger})
    assert certified_bands().exact == 0.95  # the defaults, unchanged
    note = uncalibrated_bands_note() or ""
    assert "not calibrated for embedder 'BAAI/bge-large-en-v1.5'" in note
    assert "exact 0.95" in note and "BAAI/bge-small-en-v1.5" in note
    assert apply_engine.certified_matching_warning(2) == note


@needs_db
def test_plan_and_apply_name_the_gap(monkeypatch: pytest.MonkeyPatch, org) -> None:  # noqa: F811
    _oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", registry.resolve_embedder)
    _installed(monkeypatch)
    monkeypatch.setattr(settings, "providers", {})
    files = _files([{"question": _Q, "sql": _SQL}])
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files})
    assert plan.status_code == 200, plan.text
    assert "certified matching will not run on this install" in plan.text
    applied = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    assert applied.status_code == 200, applied.text
    assert "certified matching is OFF: no embedding provider" in applied.text
    assert "dst-core[local-embed]" in applied.text
