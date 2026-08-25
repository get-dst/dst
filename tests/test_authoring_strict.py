"""A misspelled authored key is a NAMED error — never a silent no-op.

The expensive class: `dimensions:` parses, validates, compiles and never
reaches the model; `use_case:` for `use_cases:` and `descriptions:` for
`description:` apply clean and do nothing. Every file a human writes rejects
unknown keys, naming the file, the key path, and the nearest valid key.

The other half is here too: the same strictness must NOT reach the storage
schema. LensConfig and SemanticModel double as the shape lens bundles round-trip
through out of Postgres, and `lens_version.bundle_json` is immutable history —
a strict read there turns any older bundle into a 500 instead of a parse error
on one file.
"""

from __future__ import annotations

import pytest

from services.certdefs import CertifiedDefinition, parse_definition_page
from services.contracts.authoring import collapse_notes
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import SemanticModel
from services.lenses.store import LensBundle
from services.project.loader import load_lens_source
from services.project.schema import parse_project_yaml
from services.semantic.files import parse_semantic_file

LENS = "name: demo\ndisplay_name: Demo\n"
ENTITY = "name: orders\nsource:\n  connection: wh\n  table: orders\n"


def _lens(extra: str) -> dict[str, str]:
    return {"lens.yaml": LENS + extra}


# ── every authored surface rejects an unknown key, with a suggestion ───────────


@pytest.mark.parametrize(
    ("label", "call", "key", "suggestion"),
    [
        (
            "lens.yaml root",
            lambda: load_lens_source(_lens("descriptions: x\n")),
            "descriptions",
            "description",
        ),
        (
            "lens.yaml model",
            lambda: load_lens_source(_lens("model:\n  temprature: 0.3\n")),
            "temprature",
            "temperature",
        ),
        (
            "lens.yaml access",
            lambda: load_lens_source(_lens("access:\n  allow:\n    - group: a\n      caler: b\n")),
            "caler",
            "caller",
        ),
        (
            "lens.yaml select",
            lambda: load_lens_source(_lens("select:\n  entitys: [a]\n")),
            "entitys",
            "entities",
        ),
        (
            "lens.yaml logging",
            lambda: load_lens_source(_lens("logging:\n  log_sample: true\n")),
            "log_sample",
            "log_samples",
        ),
        (
            "queries.yaml top",
            lambda: load_lens_source({**_lens(""), "queries.yaml": "use_whens: [a]\n"}),
            "use_whens",
            "use_when",
        ),
        (
            "queries.yaml sample",
            lambda: load_lens_source(
                {
                    **_lens(""),
                    "queries.yaml": "sample_queries:\n  - question: q\n    sqll: SELECT 1\n",
                }
            ),
            "sqll",
            "sql",
        ),
        (
            "certified_answers.yaml",
            lambda: load_lens_source(
                {
                    **_lens(""),
                    "certified_answers.yaml": "- question: q\n  sql: SELECT 1\n  verifed_by: me\n",
                }
            ),
            "verifed_by",
            "verified_by",
        ),
        (
            "lens definitions/*.md",
            lambda: load_lens_source(
                {**_lens(""), "definitions/x.md": "---\nterm: t\nsumary: s\n---\n\nbody"}
            ),
            "sumary",
            "summary",
        ),
        (
            "entity root",
            lambda: parse_semantic_file("semantic/entities/o.yaml", ENTITY + "dimension: []\n"),
            "dimension",
            "dimensions",
        ),
        (
            "entity root (the real one)",
            lambda: parse_semantic_file("semantic/entities/o.yaml", ENTITY + "use_case: x\n"),
            "use_case",
            "use_cases",
        ),
        (
            "entity fields[]",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml",
                ENTITY + "fields:\n  - name: f\n    type: string\n    descriptions: d\n",
            ),
            "descriptions",
            "description",
        ),
        (
            "entity metrics[]",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml",
                ENTITY + "metrics:\n  - name: m\n    aggregation: sum\n    expr: o.x\n",
            ),
            "aggregation",
            "agg",
        ),
        (
            "entity joins[]",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml",
                ENTITY + "joins:\n  - right: p\n    on: a = b\n    relation: many_to_one\n",
            ),
            "relation",
            "relationship",
        ),
        (
            "entity source",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml", "name: o\nsource:\n  connection: c\n  tabel: t\n"
            ),
            "tabel",
            "table",
        ),
        (
            "semantic definition",
            lambda: parse_semantic_file(
                "semantic/definitions/d.md", "---\nterm: t\ngrian: g\n---\n\nbody"
            ),
            "grian",
            "grain",
        ),
        (
            "dst.yaml root",
            lambda: parse_project_yaml("connection: {}\n"),
            "connection",
            "connections",
        ),
        (
            "dst.yaml connection",
            lambda: parse_project_yaml("connections:\n  w:\n    typ: duckdb\n"),
            "typ",
            "type",
        ),
        (
            "dst.yaml provider",
            lambda: parse_project_yaml(
                "providers:\n  p:\n    type: openai-compatible\n    base_urls: http://x\n"
            ),
            "base_urls",
            "base_url",
        ),
    ],
)
def test_unknown_key_is_a_named_error(label: str, call: object, key: str, suggestion: str) -> None:
    with pytest.raises((ValueError, KeyError)) as exc:
        call()  # type: ignore[operator]
    message = str(exc.value)
    assert key in message, f"{label}: the error must name the offending key"
    assert f"did you mean `{suggestion}`" in message, f"{label}: no suggestion in {message!r}"


def test_the_error_names_the_file_and_the_key_path() -> None:
    """Three facts, because a schema error that only says 'extra input' costs an
    author a bisect: which file, where in it, and what to type instead."""
    with pytest.raises(ValueError) as exc:
        load_lens_source(_lens("access:\n  allow:\n    - group: a\n      caler: b\n"))
    message = str(exc.value)
    assert message.startswith("lens.yaml:")
    assert "access.allow.0" in message
    assert "did you mean `caller`" in message
    assert "keys here: caller, group" in message


# ── a REQUIRED field with no default is announced, missing it is a named error ──


@pytest.mark.parametrize(
    ("label", "call", "at", "example"),
    [
        (
            "lens.yaml name",
            lambda: load_lens_source({"lens.yaml": "connections: [wh]\n"}),
            "name",
            "e.g. sales",
        ),
        (
            "entity source.connection (nested model)",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml", "name: o\nsource:\n  table: main.o\n"
            ),
            "source.connection",
            "connection name",
        ),
        (
            "queries.yaml sample_queries[].sql",
            lambda: load_lens_source(
                {**_lens(""), "queries.yaml": "sample_queries:\n  - question: q\n"}
            ),
            "sql",
            "answers `question`",
        ),
        (
            "entity fields[].name",
            lambda: parse_semantic_file(
                "semantic/entities/o.yaml", ENTITY + "fields:\n  - type: string\n"
            ),
            "name",
            "e.g. order_id",
        ),
    ],
)
def test_a_missing_required_field_is_a_named_actionable_error(
    label: str, call: object, at: str, example: str
) -> None:
    """The other half of the unknown-key seam: `Field required` named neither the
    file, the fix, nor an example (the batch-3 `display_name: Field required` paper
    cut writ small). A missing required field now reads like the `did you mean`
    errors beside it — file, key path, that it is REQUIRED, and (where the field
    documents one) an example value. Nested and list-item paths resolve too."""
    with pytest.raises(ValueError) as exc:
        call()  # type: ignore[operator]
    message = str(exc.value)
    assert at in message, f"{label}: names the key path — got {message!r}"
    assert "required" in message, f"{label}: says it is required — got {message!r}"
    assert example in message, f"{label}: carries the example — got {message!r}"
    assert "Field required" not in message, f"{label}: the bare pydantic string is gone"


def test_a_reference_only_minimal_lens_plans_clean() -> None:
    """The invariant behind the paper cut: a lens authored from ONLY the fields the
    generated reference flags as required must plan clean. The reference marks name
    (`<required>`) and connections (`REQUIRED`) and says display_name defaults to
    name — so a minimal `name` + `connections` lens loads (display_name = name),
    plans as a clean create, and the plan after apply is unchanged (the defaulted
    display_name canonicalizes on both sides, so no phantom diff)."""
    from services.contracts.lens_config import LensConfig
    from services.project.plan import _canonical_lens_yaml, plan_lenses
    from services.project.template import reference_section

    ref = reference_section("Reference: every lens field", LensConfig)
    assert "# name: <required>" in ref  # the author must fill this
    assert "# connections: []  # REQUIRED" in ref  # and this
    assert "# display_name: <required>" not in ref  # but NOT this any more
    assert "defaults to `name`" in ref  # the reference says so out loud

    minimal = "name: sales\nconnections: [warehouse]\n"  # every required field, nothing else
    src = load_lens_source({"lens.yaml": minimal})
    assert src.config.display_name == src.config.name == "sales"

    created = plan_lenses({}, {"sales": {"lens.yaml": minimal}})
    assert [(p.lens, p.status) for p in created] == [("sales", "create")]
    db = {"sales": {"lens.yaml": _canonical_lens_yaml(minimal)}}  # what apply stores
    after = plan_lenses(db, {"sales": {"lens.yaml": minimal}})
    assert [(p.lens, p.status) for p in after] == [("sales", "unchanged")]


def test_an_alias_is_not_an_unknown_key() -> None:
    """`condition:` for a join's `on:`, and `term:`/`sql_expr:` on a definition
    page, are documented spellings — rejecting them would be the false positive
    this pass exists to avoid."""
    entity = parse_semantic_file(
        "semantic/entities/o.yaml",
        ENTITY + "joins:\n  - right: p\n    condition: a = b\n",
    )
    assert entity is not None
    page = parse_definition_page(
        "---\nterm: repeat_customer\nsql_expr: c.orders > 1\n---\n\nbody",
        path="semantic/definitions/x.md",
    )
    assert (page.metric, page.sql) == ("repeat_customer", "c.orders > 1")


def test_body_is_not_a_frontmatter_key() -> None:
    """The prose below the --- IS the body; a `body:` key up there is silently
    overwritten by it, which is the exact silent-drop this seam ends."""
    with pytest.raises(ValueError, match="not a frontmatter key"):
        parse_definition_page("---\nterm: t\nbody: nope\n---\n\nreal body", path="d.md")


@pytest.mark.parametrize(
    "page", ["no frontmatter at all", "---\n- a\n- b\n---\n\nbody", "---\nsummary: s\n---\n\nbody"]
)
def test_every_definition_page_error_names_the_page(page: str) -> None:
    """The callers now re-raise a ValueError untouched, trusting this seam to
    have named the file — so a message that forgot to would lose the filename
    across a whole semantic/ tree. Pinned so the trust stays earned."""
    with pytest.raises(ValueError, match=r"^semantic/definitions/x\.md: "):
        parse_semantic_file("semantic/definitions/x.md", page)


# ── the split: strict at the authoring seam, tolerant from storage ─────────────


def test_storage_reads_stay_tolerant() -> None:
    """A bundle written by another build carries keys this one does not know.
    lens_version.bundle_json is immutable history — reading it must not 500."""
    stored = {
        "config": {"name": "l", "display_name": "L", "canon_dir": "/legacy/path"},
        "semantic_model": {"lens": "l", "dialect": "duckdb", "version": 1, "future_key": []},
    }
    bundle = LensBundle.model_validate(stored)
    assert bundle.config.name == "l"
    assert bundle.semantic_model.dialect == "duckdb"
    # …and the same shapes validated directly are equally tolerant.
    assert LensConfig.model_validate({"name": "l", "display_name": "L", "gone": 1}).name == "l"
    assert SemanticModel.model_validate({"lens": "l", "dialect": "duckdb", "gone": 1}).lens == "l"


def test_a_valid_tree_still_loads_with_every_live_key() -> None:
    """The canary shape: nothing a real project authors is newly rejected."""
    source = load_lens_source(
        {
            "lens.yaml": (
                "name: demo\ndisplay_name: Demo\ndescription: d\nconnections: [wh]\n"
                "skills: [s]\nselect:\n  entities:\n    - name: orders\n  definitions: ['*']\n"
                "model:\n  provider: p\n  model: m\n  temperature: 0.2\n"
                "  max_rows_to_return: 10\n  max_rows_to_compose: 5\n  max_repairs: 1\n"
                "  inline_judge: false\n  adversarial_review: false\n  answer_mode: balanced\n"
                "  answer_contract: strict\n  certified_dir: /tmp/c\n"
                "instructions: go\naccess:\n  allow:\n    - caller: bench\n"
                "logging:\n  log_samples: false\nrate_limit:\n  per_caller_rpm: 60\n"
                "eval_gate: warn\nauto_review: partial\nserve_ungoverned_shapes: false\n"
            ),
            "queries.yaml": (
                "use_when:\n  - when?\nsample_queries:\n  - question: q\n    sql: SELECT 1\n"
            ),
            "certified_answers.yaml": (
                "- question: q\n  sql: SELECT 1\n  created_by: me\n  created_at: now\n"
                "  source: s\n  verified_by: v\n  status: active\n"
            ),
            "definitions/x.md": "---\nterm: t\nsummary: s\ngrain: g\nsources: [a]\n---\n\nbody",
        }
    )
    assert source.config.name == "demo"
    assert source.local_definitions[0].summary == "s"


# ── inert keys warn, they never error ─────────────────────────────────────────


def test_a_real_but_unread_key_warns_instead_of_failing() -> None:
    """The retired `context:` block is still present in older trees, so
    rejecting it would break them and ignoring it would hide the retirement.
    It parses, warns once, and never renders."""
    source = load_lens_source(_lens("context:\n  uploads: [a.md]\n  github:\n    - repo: o/r\n"))
    notes = " ".join(source.notes)
    assert "`context`" in notes and "retired" in notes and "POST" in notes
    assert "context" not in source.config.model_dump(mode="json", exclude_none=True)


def test_a_key_that_became_live_no_longer_warns() -> None:
    """`model.temperature` was on the dead list and was IMPLEMENTED instead (an
    explicit value now wins over answer_mode). The inventory has to track that —
    a warning that outlives its bug teaches authors to ignore warnings."""
    source = load_lens_source(_lens("model:\n  temperature: 0.9\n"))
    assert source.config.model.generation_temperature() == 0.9
    assert not any("temperature" in note for note in source.notes)


def test_certified_only_frontmatter_keys_name_the_directory_that_honours_them() -> None:
    page = "---\nterm: t\nquestion: how many?\nusage_mode: search\nowner: data-team\n---\n\nbody"
    notes: list[str] = []
    parse_semantic_file("semantic/definitions/d.md", page, notes=notes)
    joined = " ".join(notes)
    assert "certified_dir" in joined
    assert "`question`" in joined and "`usage_mode`" in joined
    assert "`owner`" in joined


def test_repeated_notes_collapse_to_one_counted_line() -> None:
    """20 pages authoring the same inert key is how a warnings block becomes
    something readers learn to skip."""
    notes = [f"semantic/definitions/{i}.md: `owner` does nothing" for i in range(3)]
    collapsed = collapse_notes(notes)
    assert len(collapsed) == 1
    assert collapsed[0].startswith("3 files — ")
    assert all(f"{i}.md" in collapsed[0] for i in range(3))


def test_a_definition_page_means_the_same_thing_in_either_directory() -> None:
    """summary/grain/sources used to render into the prompt for a page under
    model.certified_dir and be DROPPED for the identical file under
    semantic/definitions/ — one format, two meanings, decided by the folder."""
    page = "---\nterm: t\nsummary: s\ngrain: one row per x\nsources: [a, b]\n---\n\nbody"
    certified = parse_definition_page(page)
    shared = parse_semantic_file("semantic/definitions/t.md", page)
    assert isinstance(certified, CertifiedDefinition)
    assert shared is not None and not isinstance(shared, CertifiedDefinition)
    assert (shared.summary, shared.grain, shared.sources) == (
        certified.summary,
        certified.grain,
        certified.sources,
    )

    from services.runtime.generator import serialize_model

    rendered = serialize_model(
        SemanticModel.model_validate(
            {"lens": "l", "dialect": "duckdb", "definitions": [shared.model_dump()]}
        )
    )
    assert "grain: one row per x" in rendered
    assert "sources: a, b" in rendered


# ── YAML 1.1 bool-ish literals ───────────────────────────────────────────────


def test_bare_off_is_accepted_for_string_enums() -> None:
    """YAML 1.1 parses bare `off` as False, so `answer_contract: off` is
    rejected with an error naming the exact literal the author wrote. The loader
    coerces the boolean back to its spelling for every off-valued enum."""
    import yaml

    cfg = LensConfig.model_validate(
        yaml.safe_load(
            "name: t\ndisplay_name: T\nconnections: [wh]\n"
            "model:\n  answer_contract: off\n"
            "eval_gate: off\nauto_review: off\n"
        )
    )
    assert cfg.model.answer_contract == "off"
    assert cfg.eval_gate == "off"
    assert cfg.auto_review == "off"


def test_quoted_off_still_works() -> None:
    import yaml

    cfg = LensConfig.model_validate(
        yaml.safe_load("name: t\ndisplay_name: T\nconnections: [wh]\neval_gate: 'off'\n")
    )
    assert cfg.eval_gate == "off"


def test_reference_block_choice_literals_round_trip() -> None:
    """Every value printed in a commented reference must be valid when
    uncommented verbatim — the comment listed `(strict | off)` unquoted, so
    copying it produced an invalid file."""
    import yaml

    from services.contracts.lens_config import ModelConfig
    from services.project.template import commented_block

    block = commented_block(LensConfig)
    assert "'off'" in block  # bool-ish choice words render quoted
    # And the loader-side coercion means even the bare form works:
    assert ModelConfig.model_validate(yaml.safe_load("answer_contract: off")).answer_contract == (
        "off"
    )
