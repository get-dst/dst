"""The prompt preview — what the model actually sees, before paying for a lap.

The regression this file exists to pin: a `dimensions:` block that parses,
validates, applies and lands in compiled.yaml while never reaching the
generation prompt, with nothing in the product able to show it. A dimension an
author writes is VISIBLE here, or this test fails.

The preview costs nothing (assembly already built every piece), so the checks
below run with no embedder, no LLM and no warehouse.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SampleQuery,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.runtime.preview import PromptAsset, PromptPreview, escalation_only, preview

_ORG = "00000000-0000-0000-0000-000000000000"
_Q = "how many severe cases per month?"

client = TestClient(app)


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@pytest.fixture(autouse=True)
def _no_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    # No embedder → retrieval and certified lookup no-op, so nothing touches the
    # DB and an ambient key can't turn the preview into a live call.
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)


def _bundle(
    *, metrics: list[Metric] | None = None, use_when: list[str] | None = None
) -> LensBundle:
    cases = Entity(
        name="cases",
        description="One row per reported case.",
        source=EntitySource(connection="wh", table="public.cases"),
        default_time_field="reported_at",
        common_questions=[f"question {i}" for i in range(6)],
        fields=[
            Field(name="case_id", type="integer"),
            Field(name="patient_id", type="integer"),
            Field(name="severity", type="integer"),
            Field(name="reported_at", type="timestamp"),
        ],
        dimensions=[
            Dimension(
                name="severity_band",
                expr="CASE WHEN cases.severity >= 3 THEN 'severe' ELSE 'mild' END",
                description="Severity bucketed the way the tox team reports it.",
            )
        ],
        metrics=metrics or [],
    )
    patients = Entity(
        name="patients",
        source=EntitySource(connection="wh", table="public.patients"),
        fields=[Field(name="patient_id", type="integer")],
    )
    return LensBundle(
        config=LensConfig(name="tox", display_name="Tox"),
        semantic_model=SemanticModel(
            lens="tox",
            dialect="duckdb",
            entities=[cases, patients],
            joins=[
                Join(left="cases", right="patients", on="cases.patient_id = patients.patient_id")
            ],
            definitions=[
                Definition(term="severe case", body="severity >= 3", sql_expr="cases.severity >= 3")
            ],
            sample_queries=[SampleQuery(question="every severe case", sql="SELECT 1")],
            use_when=use_when or [],
            ai_instructions="Report case counts as integers.",
        ),
    )


def _asset(result: PromptPreview, kind: str, name: str) -> PromptAsset:
    return next(a for a in result.assets if a.kind == kind and a.name == name)


def test_an_authored_dimension_is_visible_in_the_prompt() -> None:
    """THE regression. A dimension that parses and applies but never reaches the
    model is exactly the bug this surface exists to expose — so the preview must
    show the dimension, its expression, and its description."""
    result = preview(_bundle(), _Q, _ORG)
    assert "dimension cases.severity_band" in result.system
    assert "CASE WHEN cases.severity >= 3 THEN 'severe' ELSE 'mild' END" in result.system
    assert "Severity bucketed the way the tox team reports it." in result.system
    dim = _asset(result, "dimension", "cases.severity_band")
    assert dim.in_prompt is True


def test_every_authored_asset_is_accounted_for() -> None:
    result = preview(_bundle(), _Q, _ORG)
    for kind, name in (
        ("entity", "cases"),
        ("field", "cases.severity"),
        ("dimension", "cases.severity_band"),
        ("definition", "severe case"),
        ("join", "cases -> patients"),
        ("sample_query", "every severe case"),
        ("instructions", "ai_instructions"),
    ):
        assert _asset(result, kind, name).in_prompt is True, (kind, name)


def test_an_absence_names_what_dropped_it() -> None:
    """The trim is real: serialize_model renders five common_questions per entity
    and silently drops the rest. An author who wrote six gets told which one."""
    result = preview(_bundle(use_when=["how bad was last month"]), _Q, _ORG)
    absent = {(a.kind, a.name): a.note for a in result.assets if not a.in_prompt}
    assert "trimmed" in (absent[("common_question", "cases: question 5")] or "")
    assert ("common_question", "cases: question 4") not in absent  # the first five are rendered
    # use_when is load-bearing for the ROUTER and for nothing the SQL model sees.
    router_only = [note for (kind, _n), note in absent.items() if kind == "use_when"]
    assert router_only and "router" in (router_only[0] or "")


def test_a_shared_asset_the_lens_did_not_select_is_reported_as_such() -> None:
    result = preview(_bundle(), _Q, _ORG, unselected=[("definition", "churn")])
    churn = _asset(result, "definition", "churn")
    assert churn.in_prompt is False
    assert "select" in (churn.note or "")


def test_a_metric_lens_previews_the_prompt_the_model_actually_gets_first() -> None:
    """With a metric layer the FIRST pass is the intent tier, whose prompt is a
    different, leaner serialization — an author reading only serialize_model would
    be reading a prompt the model may never be sent."""
    result = preview(_bundle(metrics=[Metric(name="case_count", agg="count")]), _Q, _ORG)
    assert result.generator_tier == "intent"
    assert result.intent_system is not None
    assert "case_count" in result.intent_system
    # The join graph and the sample queries exist only in the raw-SQL prompt.
    assert _asset(result, "join", "cases -> patients").note is not None
    assert "escalates" in (_asset(result, "sample_query", "every severe case").note or "")


def test_a_lens_without_metrics_previews_the_grounded_prompt() -> None:
    result = preview(_bundle(), _Q, _ORG)
    assert result.generator_tier == "grounded"
    assert result.intent_system is None
    assert result.counts["context_chunks"] == 0  # no embedder, no retrieval — stated, not hidden


def test_instructions_stay_out_of_the_first_pass_and_that_is_deliberate() -> None:
    """`instructions:` reaching the model only on escalation is a DECISION, not an
    oversight — so it is pinned here with the reason behind it.

    Rendering it in the lean pass changes no answer: QueryIntent cannot express
    what these rulings ask for (no DISTINCT, no "order by a count you do not
    project"), so the pass would read the rule and still be unable to obey it."""
    result = preview(_bundle(metrics=[Metric(name="case_count", agg="count")]), _Q, _ORG)
    assert result.intent_system is not None
    assert "Report case counts as integers." not in result.intent_system
    assert "Report case counts as integers." in result.system  # the tier that CAN act on it


def test_a_metric_lens_is_told_what_its_first_pass_drops() -> None:
    """The silence, killed at the authoring surface: what the lean pass does not
    render is named per lens (apply surfaces this list as a warning). Derived from
    the rendered prompts, so it cannot drift away from what is actually true."""
    dropped = escalation_only(
        _bundle(metrics=[Metric(name="case_count", agg="count")]).semantic_model
    )
    for key in ("instructions", "joins", "sample_queries", "common_questions"):
        assert any(key in d for d in dropped), (key, dropped)
    # A definition's enforceable predicate is NOT among the dropped now: the intent
    # tier can apply it (`definitions:`), so it reaches the first pass, not only the
    # escalation. This is the silent-wrong class — an edited definition that never
    # changed a metric-lens answer because its sql_expr was escalation-only.
    assert not any("definition sql_expr" in d for d in dropped), dropped
    # No metric layer, no first pass to lose anything on.
    assert escalation_only(_bundle().semantic_model) == []


def test_a_governed_definitions_predicate_reaches_the_metric_layer_first_pass() -> None:
    """The silent-wrong class, at the surface that catches it. A metric
    lens generates on the intent tier; its prompt rendered a definition's BODY but
    not its sql_expr, so editing `status_code = 'X'` to `'C'` could not change the
    answer. The enforceable half must ride the first pass, and the preview must say
    so — `dst lens prompt` is exactly where an author checks this."""
    bundle = _bundle(metrics=[Metric(name="case_count", agg="count")])
    result = preview(bundle, _Q, _ORG)
    assert result.intent_system is not None
    assert "cases.severity >= 3" in result.intent_system  # the predicate, not just the body
    sql_asset = _asset(result, "definition sql", "severe case [sql]")
    assert sql_asset.in_prompt and sql_asset.note is None  # reaches the model, not escalation-only


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Preview') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_the_endpoint_previews_a_draft_before_it_is_ever_published() -> None:
    """The authoring loop is edit → preview, so the preview reads the live bundle:
    the draft when that is all there is."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        created = client.post("/mgmt/lenses", json=_bundle().model_dump(mode="json"), headers=h)
        assert created.status_code == 201
        r = client.get("/mgmt/lenses/tox/prompt", params={"q": _Q}, headers=h)
        assert r.status_code == 200
        body = r.json()
        assert "dimension cases.severity_band" in body["system"]
        assert body["generator_tier"] == "grounded" and body["prompt_hash"]
        assert any(a["kind"] == "dimension" and a["in_prompt"] for a in body["assets"])
        assert (
            client.get("/mgmt/lenses/nope/prompt", params={"q": _Q}, headers=h).status_code == 404
        )
    finally:
        _cleanup(org)


def test_the_preview_endpoint_requires_auth() -> None:
    assert client.get("/mgmt/lenses/tox/prompt", params={"q": _Q}).status_code == 401


def test_the_second_model_call_is_rendered_too() -> None:
    """A served request makes TWO model calls and this surface rendered one. The
    composer — the call that writes the English a person reads — was sent no
    semantic model at all, and a lens whose definition forbade the exact sentence
    it produced had nowhere to see that. Both calls, or the next one hides here."""
    bound = Definition(term="severity code", about="cases.severity", body="3 and up is severe.")
    bundle = _bundle()
    bundle.semantic_model.definitions.append(bound)
    result = preview(bundle, _Q, _ORG)
    assert "grounded analytics answers" in result.compose_system
    assert "Write the answer." in result.compose_user
    # The governed page renders IN the compose turn, not merely as a checklist row.
    assert "3 and up is severe." in result.compose_user
    reach = {a.name: a for a in result.compose_assets}
    assert reach["severity code"].in_prompt is True
    assert "severity" in (reach["severity code"].note or "")
    # An `about`-less page can only reach it by a naming coincidence — say so.
    assert reach["severe case"].in_prompt is False
    assert "about" in (reach["severe case"].note or "")
