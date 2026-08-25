"""A stored bundle must stay readable by the release that could have written it.

The one-way door this pins shut: `ModelConfig.provider`/`model`/`temperature` widened
from `str`/`float` to optional, and storage wrote literal `null` into
`lens.draft_json`, `lens.published_json` and `lens_version.bundle_json` for every lens
with no `model:` block. The previous release types those fields as plain `str`/`float`,
so it raised `ValidationError` on every read — and `plan` enumerates every published
bundle, so the whole project surface 500'd and the old code could not even repair its
own project. `lens_version.bundle_json` is immutable history, so its share of the
damage was permanent. Recovery was a `pg_restore` of a pre-upgrade dump.

`extra="forbid"` at the authoring seam (test_authoring_strict) guards the FORWARD
direction — new code reading old rows. This file guards the other one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Entity, EntitySource, SemanticModel
from services.lenses.store import LensBundle, _stored


class _PreviousModelConfig(BaseModel):
    """`ModelConfig`'s three fields as the PREVIOUS release (8276cee) declared them.

    Frozen on purpose — it is the reader we must not break, so it must not track the
    contract it is here to test. A `null` in any of the three raises here exactly as it
    did in the release that shipped it.
    """

    provider: str = Field(default="anthropic")
    model: str = Field(default="claude-sonnet-4-6")
    temperature: float = Field(default=0.2)


def _bundle(**model_kwargs: Any) -> LensBundle:
    """The shape that broke: a lens with no `model:` block of its own."""
    return LensBundle(
        config=LensConfig(name="orders_ops", display_name="Orders Ops", **model_kwargs),
        semantic_model=SemanticModel(
            lens="orders_ops",
            dialect="duckdb",
            entities=[Entity(name="orders", source=EntitySource(connection="wh", table="orders"))],
        ),
    )


def _nulls(payload: Any, path: str = "") -> list[str]:
    """Every key path under `payload` whose value is JSON null."""
    if isinstance(payload, dict):
        out = []
        for key, value in payload.items():
            here = f"{path}.{key}" if path else key
            out += [here] if value is None else _nulls(value, here)
        return out
    if isinstance(payload, list):
        return [p for i, v in enumerate(payload) for p in _nulls(v, f"{path}[{i}]")]
    return []


def test_stored_bundle_carries_no_nulls() -> None:
    """The storage serialization omits unset keys — at every depth, not just model."""
    assert _nulls(json.loads(_stored(_bundle()))) == []


def test_previous_release_reads_a_bundle_this_one_wrote() -> None:
    """The downgrade: old contract, new row. It must load, on its own defaults."""
    stored = json.loads(_stored(_bundle()))
    previous = _PreviousModelConfig.model_validate(stored["config"]["model"])
    assert (previous.provider, previous.model, previous.temperature) == (
        "anthropic",
        "claude-sonnet-4-6",
        0.2,
    )


def test_a_null_in_that_payload_is_what_the_previous_release_rejects() -> None:
    """The regression this file exists for, shown failing under the old contract —
    so a future `model_dump_json()` without `exclude_none` cannot pass silently."""
    with pytest.raises(ValidationError):
        _PreviousModelConfig.model_validate({"provider": None, "model": None, "temperature": None})


def test_round_trip_through_the_current_contract_is_exact() -> None:
    """Omitting a key must not change what THIS release reads back, set or unset."""
    for bundle in (_bundle(), _bundle(model={"model": "claude-sonnet-4-6", "temperature": 0.0})):
        assert LensBundle.model_validate(json.loads(_stored(bundle))) == bundle


def test_every_optional_bundle_field_defaults_to_none() -> None:
    """The invariant that makes `exclude_none` safe, checked over the whole bundle
    rather than the three fields that broke: if some future optional field defaulted
    to a VALUE, dropping an explicit `None` would silently change it on reload."""
    seen: set[type[BaseModel]] = set()
    offenders: list[str] = []

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            annotation = field.annotation
            if "None" in str(annotation) and field.default is not None:
                offenders.append(f"{model.__name__}.{name}")
            for candidate in (annotation, *(getattr(annotation, "__args__", ()) or ())):
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    walk(candidate)

    walk(LensBundle)
    assert len(seen) > 10  # the walk actually descended
    assert offenders == []
