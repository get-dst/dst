"""What a lens knows, served with zero LLM calls and zero warehouse hits.

``GET /v1/lenses/{name}`` and the MCP ``describe_lens`` tool are thin wrappers
over ``describe_model``: entities, typed fields, business definitions, the lens
author's standing orders, and the size of the certified library. Pure — a
``SemanticModel`` in, a ``LensDescription`` out, no I/O — so any caller can
build one, including harnesses that hold a model but no HTTP stack.

``instructions`` is the field that used to be missing: the lens author's
standing orders already reach the *generator* on every ask, but never reached
the caller. An agent that can read the rules it will be judged by can phrase
its ask to match; keeping them secret bought nothing.
"""

from __future__ import annotations

from pydantic import BaseModel

from services.contracts.semantic_model import SemanticModel


class LensFieldInfo(BaseModel):
    name: str
    type: str
    description: str | None = None


class LensEntityInfo(BaseModel):
    name: str
    description: str | None = None
    fields: list[LensFieldInfo]


class LensDefinitionInfo(BaseModel):
    term: str
    body: str
    status: str = "active"  # "ambiguous" ⇒ asking about it triggers a clarification
    possible_mappings: list[str] = []


class LensDescription(BaseModel):
    name: str
    display_name: str
    description: str
    dialect: str
    entities: list[LensEntityInfo]
    definitions: list[LensDefinitionInfo]
    sample_questions: list[str]
    # The lens author's standing orders (SemanticModel.ai_instructions) — the
    # answering practice this lens enforces. Advertised so a caller can see the
    # rules before it pays for an ask.
    instructions: str | None = None
    # The certified library, advertised so agents know a deterministic path exists
    # before paying for generation.
    certified_count: int = 0
    certified_examples: list[str] = []


def describe_model(
    name: str,
    *,
    display_name: str,
    description: str,
    model: SemanticModel,
    certified_count: int = 0,
    certified_examples: list[str] | None = None,
) -> LensDescription:
    """Project a compiled semantic model into the caller-facing description."""
    return LensDescription(
        name=name,
        display_name=display_name,
        description=description,
        dialect=model.dialect,
        entities=[
            LensEntityInfo(
                name=e.name,
                description=e.description,
                fields=[
                    LensFieldInfo(name=f.name, type=f.type, description=f.description)
                    for f in e.fields
                ],
            )
            for e in model.entities
        ],
        definitions=[
            LensDefinitionInfo(
                term=d.term,
                body=d.body,
                status=d.status,
                possible_mappings=d.possible_mappings,
            )
            for d in model.definitions
        ],
        sample_questions=[s.question for s in model.sample_queries],
        instructions=model.ai_instructions,
        certified_count=certified_count,
        certified_examples=list(certified_examples or []),
    )
