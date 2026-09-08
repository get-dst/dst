"""The project-level shared semantic layer.

Entities/definitions live once, at project scope (`semantic/` on disk, the
`semantic_asset` table in the DB); a lens SELECTS them and apply compiles the
selection + lens-local extras into the embedded SemanticModel the runtime
already consumes. These are the shapes of the shared side and the selection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field as PField
from pydantic import ValidationInfo, field_validator, model_validator

from services.contracts.authoring import Authored, authoring_scope
from services.contracts.semantic_model import (
    _ON_ALIASES,
    _ON_DESC,
    Definition,
    Entity,
    StrList,
    _restore_on_key,
)


def relationship_name(left: str, right: str) -> str:
    """The asset name a relationship is stored and hashed under. Derived from
    the pair, never authored — the same pair can have exactly one identity."""
    return f"{left}__{right}"


class SharedRelationship(Authored):
    """A join pair, authored in its OWN file (semantic/relationships/*.yaml) —
    a relationship is a fact about two entities, so it lives in neither.
    Compile emits it as a model-level Join when a lens selects both endpoints."""

    left: str = PField(description="the FK-side entity (the many side of many_to_one)")
    right: str = PField(description="the entity `left` joins to")
    on: str = PField(validation_alias=_ON_ALIASES, description=_ON_DESC)
    type: Literal["inner", "left", "right", "full"] = "left"
    relationship: Literal["one_to_one", "many_to_one", "one_to_many"] | None = PField(
        default=None,
        description="row-count relationship left->right — guards fan-out/double-count bugs",
    )

    @model_validator(mode="before")
    @classmethod
    def _on_key(cls, data: object) -> object:
        return _restore_on_key(data)

    @model_validator(mode="after")
    def _two_entities(self) -> SharedRelationship:
        if self.left == self.right:
            raise ValueError(
                f"left and right are both '{self.left}' — a relationship spans two "
                "entities; model a self-join as a second entity over the same table"
            )
        return self

    @property
    def name(self) -> str:
        return relationship_name(self.left, self.right)


class SharedEntity(Entity):
    """An Entity as authored in semantic/entities/<name>.yaml."""

    @model_validator(mode="before")
    @classmethod
    def _joins_moved(cls, data: object, info: ValidationInfo) -> object:
        # Authoring seam only (storage stays tolerant): an old-style `joins:`
        # block gets the pointer, not a generic unknown-key error.
        if authoring_scope(info) is not None and isinstance(data, dict) and "joins" in data:
            raise ValueError(
                "`joins` no longer lives on the entity — a relationship is a fact "
                "about a pair, so it gets its own file: "
                "semantic/relationships/<left>__<right>.yaml with "
                "left/right/on/type/relationship (left = this FK-side entity)"
            )
        return data


class SelectEntity(Authored):
    """One entity pick in a lens's select block. YAML accepts a bare string
    ("customers") for the whole entity, or a mapping with a metric subset."""

    name: str = PField(description="shared entity name; '*' selects every entity")
    metrics: StrList | None = PField(
        default=None, description="subset of the entity's metrics; omit for all"
    )

    @field_validator("name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("entity name must be non-empty")
        return v


class SelectSpec(Authored):
    """What a lens pulls from the shared layer. Definitions default EMPTY on
    purpose: every selected term becomes a router anchor for the lens, and
    all-by-default would fan every term into every lens."""

    entities: list[SelectEntity] = PField(
        default_factory=list,
        description="shared entities to include; bare string = whole entity",
    )
    definitions: StrList = PField(
        default_factory=list,
        description="shared definition terms to include; '*' = all",
    )

    @field_validator("entities", mode="before")
    @classmethod
    def _coerce_bare_names(cls, v: object) -> object:
        if isinstance(v, list):
            return [{"name": item} if isinstance(item, str) else item for item in v]
        return v


def asset_hash(body: dict[str, Any]) -> str:
    """Stable content hash for a shared asset body (file and DB agree)."""
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_ASSET_MODELS: dict[str, type[Authored]] = {
    "entity": SharedEntity,
    "definition": Definition,
    "relationship": SharedRelationship,
}


def authored_body(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """The part of a stored asset body that carries AUTHORED MEANING.

    Every field left at its schema default drops out: the author said nothing
    about it, so it says nothing about what the term means. Unknown keys drop
    too (the models tolerate them for storage; nothing reads them).
    """
    model = _ASSET_MODELS.get(kind)
    if model is None:  # not an asset kind we model — hash the body verbatim
        return body
    return model.model_validate(body).model_dump(mode="json", exclude_defaults=True)


def asset_content_hash(kind: str, body: dict[str, Any]) -> str:
    """The digest every staleness question is asked against — the one seam, so
    the file side, the DB column and a certified answer's bindings can never
    disagree about what "changed" means.

    It covers the asset's AUTHORED body, NOT its serialized model. Hashing
    ``model_dump()`` made the digest a function of the SCHEMA as well as the
    content: adding ``summary``/``grain``/``sources`` to Definition moved every
    definition's hash on upgrade, so `dst plan` reported each file
    `unchanged` and, in the same breath, every lens stale and every certified
    answer touching one in need of re-verification. A governance gate that
    fires when nothing changed teaches people to re-attest without looking,
    which is the one habit certification exists to prevent.

    So: a field the author set is meaning and is hashed; a field dst added,
    or defaulted, or reads differently than it used to, is INTERPRETATION and
    is not. Interpretation changes get announced (plan's semantics notice), not
    hashed — a hash cannot tell the difference between "the author changed the
    term" and "we changed our mind about the term", and only the first one
    invalidates a human's verification.
    """
    return asset_hash(authored_body(kind, body))
