"""A bare string on a list-typed authoring field is ONE entry — never N characters.

The repro: `use_when:` written as a plain string (no `- `) was iterated character
by character into ~300 one-character entries. Accepted, silent, and in the routing
path. Every hand-authorable list-of-strings now coerces the scalar form; lists of
MAPPINGS still reject it, because a bare string has no single-entry meaning there.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.certdefs import CertifiedDefinition, parse_definition_page
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Definition,
    Entity,
    Metric,
    SemanticModel,
)
from services.contracts.shared_semantic import SelectEntity, SelectSpec, SharedEntity
from services.project.loader import _parse_queries
from services.semantic.files import parse_semantic_file

# The sentence that got shredded: long enough that character-iteration is obvious.
SENTENCE = "Use this lens for commission questions about quarterly payouts."
_SOURCE = {"connection": "wh", "table": "orders"}


def _entity(**kw: object) -> dict[str, object]:
    return {"name": "orders", "source": _SOURCE, **kw}


def _model(**kw: object) -> dict[str, object]:
    return {"lens": "l", "dialect": "duckdb", **kw}


def _lens(**kw: object) -> dict[str, object]:
    return {"name": "l", "display_name": "L", **kw}


# --- coerce: every hand-authorable list[str] takes the scalar form as one entry ---


@pytest.mark.parametrize(
    ("build", "read"),
    [
        pytest.param(
            lambda v: SemanticModel.model_validate(_model(use_when=v)),
            lambda m: m.use_when,
            id="SemanticModel.use_when",
        ),
        pytest.param(
            lambda v: SemanticModel.model_validate(_model(excluded_metrics=v)),
            lambda m: m.excluded_metrics,
            id="SemanticModel.excluded_metrics",
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(use_cases=v)),
            lambda e: e.use_cases,
            id="Entity.use_cases",
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(common_questions=v)),
            lambda e: e.common_questions,
            id="Entity.common_questions",
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(primary_key=v)),
            lambda e: e.primary_key,
            id="Entity.primary_key",
        ),
        pytest.param(
            lambda v: Metric.model_validate({"name": "m", "agg": "count", "filters": v}),
            lambda m: m.filters,
            id="Metric.filters",
        ),
        pytest.param(
            lambda v: Definition.model_validate(
                {"term": "arr", "body": "b", "status": "ambiguous", "possible_mappings": v}
            ),
            lambda d: d.possible_mappings,
            id="Definition.possible_mappings",
        ),
        pytest.param(
            lambda v: SelectSpec.model_validate({"definitions": v}),
            lambda s: s.definitions,
            id="SelectSpec.definitions",
        ),
        pytest.param(
            lambda v: SelectEntity.model_validate({"name": "orders", "metrics": v}),
            lambda s: s.metrics,
            id="SelectEntity.metrics",
        ),
        pytest.param(
            lambda v: LensConfig.model_validate(_lens(connections=v)),
            lambda c: c.connections,
            id="LensConfig.connections",
        ),
        pytest.param(
            lambda v: LensConfig.model_validate(_lens(skills=v)),
            lambda c: c.skills,
            id="LensConfig.skills",
        ),
        pytest.param(
            lambda v: CertifiedDefinition.model_validate({"term": "arr", "sources": v}),
            lambda d: d.sources,
            id="CertifiedDefinition.sources",
        ),
        pytest.param(
            lambda v: CertifiedDefinition.model_validate({"term": "arr", "possible_mappings": v}),
            lambda d: d.possible_mappings,
            id="CertifiedDefinition.possible_mappings",
        ),
    ],
)
def test_bare_string_becomes_one_entry(build, read) -> None:  # type: ignore[no-untyped-def]
    got = read(build(SENTENCE))
    assert got == [SENTENCE], f"expected one entry, got {len(got)}"
    # the actual bug, pinned: never the character list
    assert len(got) == 1 and got[0] == SENTENCE


@pytest.mark.parametrize(
    ("build", "read"),
    [
        pytest.param(
            lambda v: SemanticModel.model_validate(_model(use_when=v)),
            lambda m: m.use_when,
            id="SemanticModel.use_when",
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(primary_key=v)),
            lambda e: e.primary_key,
            id="Entity.primary_key",
        ),
        pytest.param(
            lambda v: LensConfig.model_validate(_lens(connections=v)),
            lambda c: c.connections,
            id="LensConfig.connections",
        ),
    ],
)
def test_the_list_form_still_works_untouched(build, read) -> None:  # type: ignore[no-untyped-def]
    assert read(build(["a", "b"])) == ["a", "b"]


# --- reject: a scalar that is NOT a string has no single-entry reading ---


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda v: SemanticModel.model_validate(_model(use_when=v)), id="use_when"),
        pytest.param(lambda v: Entity.model_validate(_entity(primary_key=v)), id="primary_key"),
        pytest.param(lambda v: LensConfig.model_validate(_lens(skills=v)), id="skills"),
        pytest.param(lambda v: SelectSpec.model_validate({"definitions": v}), id="definitions"),
    ],
)
@pytest.mark.parametrize("bad", [5, 4.2, True, {"a": 1}])
def test_non_string_scalars_are_rejected(build, bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError) as exc:
        build(bad)
    assert any(e["type"] == "list_type" for e in exc.value.errors())


@pytest.mark.parametrize(
    ("build", "field"),
    [
        pytest.param(
            lambda v: SemanticModel.model_validate(_model(entities=v)), "entities", id="entities"
        ),
        pytest.param(
            lambda v: SemanticModel.model_validate(_model(sample_queries=v)),
            "sample_queries",
            id="sample_queries",
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(fields=v)), "fields", id="entity.fields"
        ),
        pytest.param(
            lambda v: Entity.model_validate(_entity(metrics=v)), "metrics", id="entity.metrics"
        ),
    ],
)
def test_lists_of_mappings_reject_a_bare_string_naming_the_field(build, field) -> None:  # type: ignore[no-untyped-def]
    """A string is not one metric/field/entity — it must NOT be coerced, and the
    error has to name the field so the author knows which line to fix."""
    with pytest.raises(ValidationError) as exc:
        build(SENTENCE)
    errors = exc.value.errors()
    assert any(e["type"] == "list_type" for e in errors)
    assert any(field in ".".join(str(p) for p in e["loc"]) for e in errors)


# --- the file surfaces the reporter actually authored ---


def test_queries_yaml_use_when_as_a_bare_string_is_one_entry() -> None:
    """THE repro: `use_when: <sentence>` in queries.yaml. Was ~300 entries."""
    use_when, _samples = _parse_queries(f"use_when: {SENTENCE}")
    assert use_when == [SENTENCE]


def test_queries_yaml_use_when_list_form_is_unchanged() -> None:
    use_when, _samples = _parse_queries("use_when:\n  - first ask\n  - second ask")
    assert use_when == ["first ask", "second ask"]


def test_queries_yaml_sample_queries_bare_string_is_rejected_with_the_shape() -> None:
    with pytest.raises(ValueError, match="sample_queries must be a list"):
        _parse_queries("sample_queries: what is arr")


def test_queries_yaml_use_when_mapping_is_rejected_with_the_shape() -> None:
    with pytest.raises(ValueError, match="use_when must be a list"):
        _parse_queries("use_when:\n  a: b")


def test_entity_file_use_cases_as_a_bare_string_is_one_entry() -> None:
    asset = parse_semantic_file(
        "semantic/entities/orders.yaml",
        f"name: orders\nsource:\n  connection: wh\n  table: orders\nuse_cases: {SENTENCE}\n",
    )
    assert isinstance(asset, SharedEntity)
    assert asset.use_cases == [SENTENCE]


def test_definition_frontmatter_possible_mappings_as_a_bare_string_is_one_entry() -> None:
    page = parse_definition_page(
        "---\nterm: arr\nstatus: ambiguous\n"
        "possible_mappings: booked - the booked view\n---\n\nprose"
    )
    assert page.possible_mappings == ["booked - the booked view"]


def test_entity_source_as_a_bare_string_names_the_shape_not_the_class() -> None:
    """The most common sibling slip: `source: warehouse`.

    Unlike a list field, this cannot be coerced — `warehouse` is a plausible
    connection and `main.orders` a plausible table — so the error has to teach the
    two-key shape rather than a pydantic class the author has never seen.
    """
    with pytest.raises(ValueError) as exc:
        parse_semantic_file("semantic/entities/orders.yaml", "name: orders\nsource: warehouse\n")
    message = str(exc.value)
    assert "connection:" in message and "table:" in message
    assert "source: warehouse" in message  # echoes what they actually wrote
    assert "EntitySource" not in message
