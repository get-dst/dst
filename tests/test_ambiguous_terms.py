"""G4 — ambiguity-aware glossary: ask, don't answer.

A definition with status="ambiguous" makes the generator emit a clarify marker
instead of SQL; the pipeline surfaces it as a clarification response (canonical
options from the model), trace.status="clarification", warehouse untouched.
"""

from __future__ import annotations

import pytest

from services.contracts.fakes import ScriptedLLM
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    SemanticModel,
)
from services.runtime.generator import (
    GenerationFormatError,
    GroundedSQLGenerator,
    parse_generation,
    serialize_model,
)
from services.runtime.pipeline import run_query

WORKLOAD = Definition(
    term="workload",
    body="ASK the user: usage, projects, or deployments?",
    status="ambiguous",
    possible_mappings=["usage - product_usage table", "projects - projects table"],
)


def _model(definitions: list[Definition]) -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="projects",
                source=EntitySource(connection="wh", table="projects"),
                primary_key=["id"],
                fields=[Field(name="id", type="string")],
            )
        ],
        definitions=definitions,
    )


class _ExplodingConnector:
    kind = "duckdb"

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ):  # pragma: no cover - must not run
        raise AssertionError("warehouse must not be touched on a clarification")


# ── serialization ────────────────────────────────────────────────────────────


def test_serialize_renders_ambiguous_block() -> None:
    prompt = serialize_model(_model([WORKLOAD]))
    assert "workload [AMBIGUOUS — MUST clarify, never guess]: ASK the user" in prompt
    assert "Options: usage - product_usage table; projects - projects table" in prompt
    # active definitions render exactly as before
    active = serialize_model(_model([Definition(term="ltv", body="prose", sql_expr="x > 1")]))
    model_section = active.split("=== Semantic model ===")[1]
    assert "- ltv: prose  [sql: x > 1]" in model_section
    assert "AMBIGUOUS" not in model_section


# ── parse ────────────────────────────────────────────────────────────────────


def test_parse_generation_clarify_marker() -> None:
    out = parse_generation(
        '{"clarify": {"term": "workload", "question": "Which meaning?", "options": ["a", "b"]}}'
    )
    assert out.sql == "" and out.clarification is not None
    assert out.clarification.term == "workload"
    assert out.clarification.options == ["a", "b"]
    # malformed clarify (not a dict) is a format error now — never raw SQL
    with pytest.raises(GenerationFormatError):
        parse_generation('{"clarify": "nonsense"}')


# ── pipeline branch ──────────────────────────────────────────────────────────


def _run(script: str, definitions: list[Definition]):
    from services.runtime.answer import AnswerComposer

    llm = ScriptedLLM([script])
    return run_query(
        question="how many workloads do we have?",
        lens_name="t",
        org_id="org-1",
        caller="test",
        semantic_model=_model(definitions),
        connector=_ExplodingConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )


def test_pipeline_returns_clarification_with_canonical_options() -> None:
    script = (
        '{"clarify": {"term": "workload", "question": '
        '"Do you mean usage or projects?", "options": ["llm-echo"]}}'
    )
    result = _run(script, [WORKLOAD])
    resp = result.response
    assert resp.clarification is not None
    assert resp.sql is None and resp.data is None
    # canonical possible_mappings replace the LLM echo; tailored question kept
    assert resp.clarification.options == WORKLOAD.possible_mappings
    assert resp.clarification.question == "Do you mean usage or projects?"
    assert resp.answer == resp.clarification.question
    assert result.trace.status == "clarification" and result.trace.error is None


def test_pipeline_fallback_question_when_llm_omits_it() -> None:
    script = '{"clarify": {"term": "workload", "question": "", "options": []}}'
    result = _run(script, [WORKLOAD])
    q = result.response.clarification.question  # type: ignore[union-attr]
    assert "'workload' is ambiguous here" in q
    assert "usage - product_usage table" in q


def test_pipeline_serves_lens_tailored_options() -> None:
    # Compile drops the mapping this lens can't serve (product_usage isn't in the
    # model) and keeps the prose meaning; the clarification offers exactly the
    # tailored list — the pipeline needs no change to pick it up.
    from services.contracts.lens_config import LensConfig
    from services.contracts.shared_semantic import SelectSpec, SharedEntity
    from services.project.compile import compile_lens_model
    from services.runtime.answer import AnswerComposer

    shared = SharedEntity(
        name="projects",
        source=EntitySource(connection="wh", table="projects"),
        primary_key=["id"],
        fields=[Field(name="id", type="string")],
    )
    ambiguous = Definition(
        term="workload",
        body="ASK the user which meaning.",
        status="ambiguous",
        possible_mappings=[
            "projects - projects",
            "usage - product_usage",
            "compute hours (ask the platform team)",
        ],
    )
    model, warnings = compile_lens_model(
        config=LensConfig(
            name="t",
            display_name="T",
            connections=["wh"],
            select=SelectSpec.model_validate(
                {"entities": ["projects"], "definitions": ["workload"]}
            ),
        ),
        shared_entities={"projects": shared},
        shared_relationships={},
        shared_definitions={"workload": ambiguous},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
    )
    assert any("'workload'" in w and "dropped" in w for w in warnings)
    llm = ScriptedLLM(
        ['{"clarify": {"term": "workload", "question": "Which?", "options": ["llm-echo"]}}']
    )
    result = run_query(
        question="how many workloads do we have?",
        lens_name="t",
        org_id="org-1",
        caller="test",
        semantic_model=model,
        connector=_ExplodingConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    clar = result.response.clarification
    assert clar is not None
    assert clar.options == ["projects - projects", "compute hours (ask the platform team)"]


def test_ambiguity_present_does_not_force_clarification() -> None:
    # The model contains an ambiguous term, but the LLM answered normally.
    script = (
        '{"sql": "SELECT COUNT(projects.id) AS n FROM projects AS projects", '
        '"definition_used": null, "rationale": "r"}'
    )

    class _CountingConnector:
        kind = "duckdb"

        def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
            from services.contracts.warehouse import QueryResult

            return QueryResult(columns=["n"], rows=[[3]])

    from services.runtime.answer import AnswerComposer

    llm = ScriptedLLM([script, "The org has 3 projects."])
    result = run_query(
        question="how many projects do we have?",
        lens_name="t",
        org_id="org-1",
        caller="test",
        semantic_model=_model([WORKLOAD]),
        connector=_CountingConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert result.response.clarification is None
    assert result.trace.status == "ok"


def test_deterministic_clarification_trigger_matrix() -> None:
    """Ask-don't-answer as code, not model behavior (matrix v2 finding: the
    intent path answered the canonical ambiguous question in every run)."""
    from services.contracts.semantic_model import Definition, SemanticModel
    from services.runtime.pipeline import deterministic_clarification

    model = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[],
        definitions=[
            Definition(
                term="value",
                body="ASK the user",
                status="ambiguous",
                possible_mappings=[
                    "lifetime value - customers.customer_lifetime_value",
                    "order amount - orders.amount",
                ],
            ),
            Definition(term="repeat_customer", body="n > 1", sql_expr="x"),
        ],
    )
    # the canonical failure: entity qualifier is not a meaning
    hit = deterministic_clarification("What is the average value of a customer?", model)
    assert hit is not None and hit.term == "value" and len(hit.options) == 2
    # naming a meaning phrase resolves it
    assert deterministic_clarification("average lifetime value of a customer?", model) is None
    assert deterministic_clarification("what was the average order amount?", model) is None
    # unrelated questions and non-ambiguous terms never trigger
    assert deterministic_clarification("how many repeat customers?", model) is None
    assert deterministic_clarification("what do we value most?", model) is not None  # honest hit
    # substring safety: 'valued' is not 'value'
    assert deterministic_clarification("most valued customers by orders?", model) is None


def test_aliases_make_the_ambiguity_reachable_for_business_phrasings() -> None:
    """Regression: identifier-only matching fires for the one phrasing the
    AUTHOR types and never for what users say, so a declared ambiguity gets
    silently guessed. Aliases widen the LEXICON (still literal word-boundary,
    zero model involvement)."""
    from services.contracts.semantic_model import Definition, SemanticModel
    from services.runtime.pipeline import deterministic_clarification

    model = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[],
        definitions=[
            Definition(
                term="feature_adoption",
                body="ASK: three flags give three different numbers",
                status="ambiguous",
                aliases=["mobile app usage", "using the mobile app", "app adoption"],
                possible_mappings=[
                    "currently using (30-day window) - usage.is_currently_using",
                    "ever used - usage.has_ever_used",
                    "has seen - usage.has_seen",
                ],
            )
        ],
    )
    # The business phrasing that used to be silently guessed…
    hit = deterministic_clarification("How many accounts are using the mobile app?", model)
    assert hit is not None and hit.term == "feature_adoption"
    # …the identifier still fires…
    assert deterministic_clarification("What is our feature adoption?", model) is not None
    # …the escape hatch survives: naming a meaning skips the clarification…
    assert deterministic_clarification("How many accounts have ever used the app?", model) is None
    # …and unrelated questions never trigger.
    assert deterministic_clarification("How many orders shipped yesterday?", model) is None


def test_apply_warns_when_an_ambiguous_term_has_no_aliases() -> None:
    # DEAD-GOVERNANCE lint: a declaration whose only trigger is its identifier
    # is unreachable in practice, so apply must say so.
    from services.contracts.lens_config import LensConfig
    from services.contracts.semantic_model import Definition, SemanticModel
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    def _bundle(defn: Definition) -> LensBundle:
        return LensBundle(
            config=LensConfig(name="t", display_name="T", connections=["wh"]),
            semantic_model=SemanticModel(
                lens="t", dialect="duckdb", entities=[], definitions=[defn]
            ),
        )

    bare = Definition(term="order_value", body="ASK", status="ambiguous")
    report = validate_bundle(_bundle(bare), [], [])
    assert any(i.code == "ambiguous_term_unreachable" for i in report.issues)

    aliased = bare.model_copy(update={"aliases": ["basket size"]})
    report = validate_bundle(_bundle(aliased), [], [])
    assert not any(i.code == "ambiguous_term_unreachable" for i in report.issues)


def test_pipeline_precheck_clarifies_before_any_generator() -> None:
    """The pre-check fires before the generator runs at all — the intent path
    can never answer past an ambiguous term again."""
    from services.contracts.semantic_model import Definition, Entity, SemanticModel
    from services.runtime.pipeline import run_query

    class ExplodingGenerator:
        model = "test"

        def generate(self, **kwargs):  # noqa: ANN003
            raise AssertionError("generator must not run for a deterministic clarify")

    class ExplodingConnector:
        kind = "duckdb"

        def execute(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("warehouse must not be touched")

    model = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity.model_validate(
                {"name": "orders", "source": {"connection": "wh", "table": "orders"}}
            )
        ],
        definitions=[
            Definition(
                term="value",
                body="ASK",
                status="ambiguous",
                possible_mappings=["lifetime value - x", "order amount - y"],
            )
        ],
    )
    result = run_query(
        question="what is the average value of a customer?",
        lens_name="t",
        org_id="00000000-0000-0000-0000-000000000000",
        caller="alex",
        semantic_model=model,
        connector=ExplodingConnector(),
        generator=ExplodingGenerator(),
        composer=None,  # type: ignore[arg-type] — never reached on a pre-check clarify
    )
    assert result.trace.status == "clarification"
    assert result.response.clarification is not None
    assert result.response.clarification.options == ["lifetime value - x", "order amount - y"]


def test_clarification_is_a_status_not_just_a_field() -> None:
    """A clarify is a governed non-answer: no SQL ran. The caller had to notice
    the `clarification` field to know that — one special case out of five, and
    the only one the old CLI branched on."""
    script = (
        '{"clarify": {"term": "workload", "question": '
        '"Do you mean usage or projects?", "options": ["llm-echo"]}}'
    )
    result = _run(script, [WORKLOAD])
    assert result.response.status == "clarification" == result.trace.status
    assert result.response.data is None


# ── deterministic resolution: the clarify rail's earliest exit ───────────────

REVENUE = Definition(
    term="revenue",
    body="ASK unless the question or the caller resolves it",
    status="ambiguous",
    possible_mappings=[
        "net invoiced revenue (Finance / CFO world) - invoices.amount, "
        "sum of invoices.amount by issue date",
        "bookings (Sales / VP Sales world) - deals.value, sum of deals.value by close date",
    ],
    audiences={
        "cfo": "net invoiced revenue",
        "finance": "net invoiced revenue",
        "head of sales": "bookings",
        "vp sales": "bookings",
    },
)


def _revenue_model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection="wh", table="invoices"),
                primary_key=["id"],
                fields=[Field(name="id", type="string"), Field(name="amount", type="number")],
            )
        ],
        definitions=[REVENUE],
    )


class _SummingConnector:
    kind = "duckdb"

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
        from services.contracts.warehouse import QueryResult

        return QueryResult(columns=["total"], rows=[[3]])


def _run_revenue(question: str):
    from services.runtime.answer import AnswerComposer

    llm = ScriptedLLM(
        [
            '{"sql": "SELECT SUM(invoices.amount) AS total FROM invoices AS invoices", '
            '"definition_used": null, "rationale": "r"}',
            "The total was 3.",
        ]
    )
    return run_query(
        question=question,
        lens_name="t",
        org_id="org-1",
        caller="test",
        semantic_model=_revenue_model(),
        connector=_SummingConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )


def test_resolution_matrix() -> None:
    """The two deterministic channels, their negatives, and the exclusions —
    a wrong auto-pick is strictly worse than a clarify, so anything short of
    exactly one selected mapping stays on the clarify rail."""
    from services.runtime import ambiguity

    model = _revenue_model()

    def picked(question: str) -> list[tuple[str, str | None]]:
        return [
            (ambiguity.mapping_label(r.mapping), r.audience)
            for r in ambiguity.resolve(question, model)
        ]

    # (1) the question contains one mapping's own decisive term — the
    # parenthetical qualifier the caller never types must not block it
    assert picked("What was net invoiced revenue in Q1 2026?") == [
        ("net invoiced revenue (Finance / CFO world)", None)
    ]
    # (2)+(3) the caller rail: the principal's identity rides the question
    # text and a declared audience phrase resolves it
    assert picked("On behalf of the CFO: what was our revenue in March 2026?") == [
        ("net invoiced revenue (Finance / CFO world)", "cfo")
    ]
    assert picked("On behalf of the Head of Sales: what was our revenue in March 2026?") == [
        ("bookings (Sales / VP Sales world)", "head of sales")
    ]
    # a caller no rule maps must NOT default — genuinely unresolved
    assert picked("On behalf of the CEO: what was our revenue in March 2026?") == []
    assert picked("what was our revenue in March 2026?") == []
    # naming both readings is still contested, and a question whose wording
    # picks one mapping while its audience picks the other never resolves
    assert picked("compare net invoiced revenue and bookings revenue for March") == []
    assert picked("On behalf of the CFO: how was our bookings revenue?") == []
    # a decisive term that IS the trigger surface cannot settle the ambiguity:
    # a mapping labeled with the bare term never auto-resolves
    bare = model.model_copy(
        update={
            "definitions": [
                Definition(
                    term="revenue",
                    body="ASK",
                    status="ambiguous",
                    possible_mappings=["revenue - invoices.amount", "bookings - deals.value"],
                )
            ]
        }
    )
    assert ambiguity.resolve("what was revenue in March?", bare) == []


def test_question_naming_one_decisive_term_serves_instead_of_clarifying() -> None:
    """The measured probe: 'net invoiced revenue' IS one mapping's own meaning,
    and the old rail clarified on the 'revenue' inside it. Resolution is pinned
    and auditable — the trace's context_refs and the citations name it — but
    stays out of the prose: the caller's own wording named the reading."""
    result = _run_revenue("What was net invoiced revenue in Q1 2026?")
    resp = result.response
    assert resp.status == "ok" and resp.clarification is None
    ref = "ambiguity-resolution: revenue -> net invoiced revenue (Finance / CFO world)"
    assert ref in result.trace.context_refs
    assert any(c.type == "context" and c.ref == ref for c in resp.citations)
    assert "Reading applied" not in resp.answer


def test_declared_audience_resolves_and_disclosure_names_the_reading() -> None:
    """The caller-rail probes: the question's wording never names a mapping, so
    the served reading is said out loud — in the prose and in trust_summary —
    exactly like an ambiguity disclosure, never silently."""
    cfo = _run_revenue("On behalf of the CFO: what was our revenue in March 2026?")
    assert cfo.response.status == "ok" and cfo.response.clarification is None
    assert (
        "ambiguity-resolution: revenue -> net invoiced revenue (Finance / CFO world)"
        in cfo.trace.context_refs
    )
    line = (
        "Reading applied: 'revenue' was read as net invoiced revenue (Finance / CFO "
        "world) — declared audience 'cfo' resolves it"
    )
    assert line in cfo.response.answer
    assert cfo.response.trust_summary is not None and line in cfo.response.trust_summary

    sales = _run_revenue("On behalf of the Head of Sales: what was our revenue in March 2026?")
    assert sales.response.status == "ok"
    assert (
        "ambiguity-resolution: revenue -> bookings (Sales / VP Sales world)"
        in sales.trace.context_refs
    )
    assert "read as bookings (Sales / VP Sales world)" in sales.response.answer


def test_unresolved_ambiguity_still_clarifies_never_defaults() -> None:
    """The negative half of the guarantee: a bare ambiguous ask, and a caller
    no audience rule maps, both still clarify — before any generator."""

    class ExplodingGenerator:
        model = "test"

        def generate(self, **kwargs):  # noqa: ANN003
            raise AssertionError("generator must not run for a deterministic clarify")

    for question in [
        "what was our revenue in March 2026?",
        "On behalf of the CEO: what was our revenue in March 2026?",
    ]:
        result = run_query(
            question=question,
            lens_name="t",
            org_id="org-1",
            caller="test",
            semantic_model=_revenue_model(),
            connector=_ExplodingConnector(),
            generator=ExplodingGenerator(),
            composer=None,  # type: ignore[arg-type] — never reached on a pre-check clarify
        )
        assert result.response.status == "clarification"
        assert result.response.clarification is not None
        assert result.response.clarification.options == REVENUE.possible_mappings
        assert not result.trace.context_refs


def test_pinned_model_steers_generation_and_stands_the_clarify_down() -> None:
    """What the generator sees: the resolved term serves as an active
    definition carrying the mapping, so the prompt steers to the reading and
    stops demanding a clarify the pre-check already settled."""
    from services.runtime import ambiguity

    model = _revenue_model()
    resolved = ambiguity.resolve("On behalf of the CFO: what was our revenue?", model)
    section = serialize_model(ambiguity.pin(model, resolved)).split("=== Semantic model ===")[1]
    assert "AMBIGUOUS" not in section
    assert (
        "'revenue' means: net invoiced revenue (Finance / CFO world) - invoices.amount" in section
    )


def test_apply_warns_when_an_audience_selects_no_single_mapping() -> None:
    # DEAD-GOVERNANCE lint, same class as the no-aliases warning: an
    # `audiences:` value selecting zero (or several) mappings never fires.
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    def _issues(defn: Definition):
        bundle = LensBundle(
            config=LensConfig(name="t", display_name="T", connections=["wh"]),
            semantic_model=SemanticModel(
                lens="t", dialect="duckdb", entities=[], definitions=[defn]
            ),
        )
        report = validate_bundle(bundle, [], [])
        return [i for i in report.issues if i.code == "ambiguous_audience_unmapped"]

    assert not _issues(REVENUE)  # every declared audience selects exactly one
    zero = REVENUE.model_copy(update={"audiences": {"board": "gross revenue"}})
    assert len(_issues(zero)) == 1
    two = REVENUE.model_copy(update={"audiences": {"cfo": "world"}})  # matches both labels
    assert len(_issues(two)) == 1


def test_audiences_round_trip_the_definition_page() -> None:
    """The authoring surface: `audiences:` frontmatter parses into the
    Definition and renders back — the page format stays the product artifact."""
    from services.semantic.files import definition_to_page, page_to_definition

    page = definition_to_page(REVENUE)
    assert "audiences:" in page and "head of sales: bookings" in page
    parsed = page_to_definition(page)
    assert parsed.audiences == REVENUE.audiences
