"""Approving a drafted patch is a RULING that proposes a file.

Files author, the UI governs. Approve writes only what is
DB-first — a certified pair — and returns everything file-owned (a definition
page, a lens.yaml ``instructions:`` edit) as a proposed file change with
``live: false``. It used to write those into the lens's draft bundle, where the next
``dst apply`` recompiled them away from the same unchanged files: the human's
ruling evaporated silently. The last test here is that repro, end to end.
"""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import HashEmbedder, ScriptedLLM, fake_llm_providers
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.semantic.files import render_semantic_files

client = TestClient(app)

CORRECTED_SQL = "SELECT count(*) FROM customers WHERE number_of_orders >= 2"
TRACE_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"  # the WRONG one
ORIGINAL_BODY = "A repeat customer has number_of_orders > 1."


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


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('ApplyT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_trace(org: object, request_id: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, certification, "
                "latency, status) VALUES "
                "(:o, :r, 'customer_value', 'agent-1', 'how many repeat customers?', "
                "'SELECT count(*) FROM customers WHERE number_of_orders > 1', true, 1, "
                "'There are 19 repeat customers.', 'repeat_customer', 'high', 'none', "
                "'{}'::jsonb, 'ok')"
            ),
            {"o": org, "r": request_id},
        )


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "patch_candidate",
            "review",
            "request_log",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _setup_ticket_with_patch(
    h: dict[str, str],
    org: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str,
    corrected_sql: str | None,
    drafter_script: str,
    create_lens: bool = True,
    target: str | None = None,
) -> dict[str, object]:
    """Lens + trace + correction-carrying ticket + drafted patch; returns the patch."""
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # One shared script across provider constructions: the judge (review open) consumes
    # the first response, the drafter the second.
    llm = ScriptedLLM(['{"verdict": "changes", "reasoning": "disputed"}', drafter_script])
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: llm)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda ref: ResolvedModel(llm, "fake", registry.wire_name(ref)),
    )
    if create_lens:
        body = jaffle_customer_value_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=body, headers=h).status_code == 201
    ticket = client.post(
        "/v1/reviews",
        headers=h,
        json={
            "request_id": rid,
            "correction": {
                "kind": kind,
                "note": "repeat_customer must be windowed to 12 months",
                "corrected_sql": corrected_sql,
                "target": target,
            },
        },
    ).json()
    drafted = client.post(f"/mgmt/reviews/{ticket['ticket_id']}/draft-patch", headers=h)
    assert drafted.status_code == 201, drafted.text
    return dict(drafted.json())


@needs_db
def test_approve_definition_patch_proposes_the_file_and_leaves_the_draft_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    new_body = "A repeat customer has 2+ orders in the last 12 months."
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,  # prose-only: corrected_sql now routes to certified
            drafter_script=f'{{"body": "{new_body}"}}',
        )
        assert cand["kind"] == "definition"
        before = client.get("/mgmt/lenses/customer_value", headers=h).json()["draft"]

        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "approved"

        # The ruling proposes the FILE the term is authored in — repeat_customer is
        # a shared term, so the shared page, not this lens's tree.
        assert out["live"] is False
        assert out["applied"] == {}  # nothing written server-side
        proposed = out["proposed_file"]
        assert proposed["path"] == "semantic/definitions/repeat-customer.md"
        assert proposed["content"].startswith("---\nmetric: repeat_customer")
        assert proposed["content"].rstrip().endswith(new_body)
        assert f"+{new_body}" in proposed["diff"]
        assert f"-{ORIGINAL_BODY}" in proposed["diff"]
        assert "dst apply" in out["next_step"] and proposed["path"] in out["next_step"]

        # …and the draft bundle is untouched: no write behind the user's back.
        lens = client.get("/mgmt/lenses/customer_value", headers=h).json()
        defs = {d["term"]: d["body"] for d in lens["draft"]["semantic_model"]["definitions"]}
        assert defs["repeat_customer"] == ORIGINAL_BODY
        # The whole draft, not one term: `version == 1` used to stand in here,
        # and it was a constant nothing could ever change.
        assert lens["draft"] == before

        # the correction became an eval case — candidate, never auto-approved
        assert out["eval_case_id"]
        cases = client.get("/mgmt/lenses/customer_value/evals/cases", headers=h).json()
        assert len(cases) == 1
        case = cases[0]
        assert case["status"] == "candidate"
        assert case["source"] == "harvested"
        assert case["question"] == "how many repeat customers?"
        # A prose-only correction has NO verified oracle. The trace's
        # sql is the WRONG answer that prompted the correction — pinning it made
        # the case pass only while the lens stayed broken, and left `dst plan`
        # permanently dirty. Harvest a behavioral pin instead.
        assert case["expected_sql"] is None
        assert case["expect"] == "answer"
        assert TRACE_SQL not in json.dumps(case)

        # one-shot: a second approve conflicts
        again = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert again.status_code == 409
    finally:
        _cleanup(org)


@needs_db
def test_approve_shared_lands_a_new_term_in_the_shared_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve always proposed lens-local for a term the
    lens had not already compiled from shared, so a ruling on a cross-cutting term
    was confined to one lens — while the context skill's blast-radius rules assume
    shared definitions are normal. `?shared=true` is the explicit promotion."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,
            drafter_script='{"body": "Overtake scope counts on-track passes only."}',
            target="overtake_scope",  # a term this lens does not carry
        )
        assert cand["kind"] == "definition" and cand["target"] == "overtake_scope"
        default = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h).json()
        assert default["proposed_file"]["path"] == (
            "lenses/customer_value/definitions/overtake-scope.md"
        )

        # the same draft, promoted: same body, the org's shared tree
        again = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,
            drafter_script='{"body": "Overtake scope counts on-track passes only."}',
            target="overtake_scope",
            create_lens=False,
        )
        promoted = client.post(
            f"/mgmt/reviews/patches/{again['id']}/approve", headers=h, params={"shared": "true"}
        ).json()
        assert promoted["proposed_file"]["path"] == "semantic/definitions/overtake-scope.md"
        assert "Overtake scope counts on-track passes only." in promoted["proposed_file"]["content"]
    finally:
        _cleanup(org)


@needs_db
def test_approve_certified_patch_certifies_corrected_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="number",
            corrected_sql=CORRECTED_SQL,
            drafter_script="unused",
        )
        assert cand["kind"] == "certified"

        # the certify apply embeds the question once — stubbed, no live embedding call
        monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: HashEmbedder())

        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["applied"]["certified_id"]
        # Certified answers are DB-first — a fileless apply keeps them — so this one
        # really is live; export round-trips it into the lens's certified_answers.yaml.
        assert out["live"] is True and out["proposed_file"] is None
        assert "dst export --lens customer_value" in out["next_step"]

        certified = client.get("/mgmt/lenses/customer_value/certified", headers=h).json()
        assert len(certified) == 1
        assert certified[0]["question"] == "how many repeat customers?"
        assert certified[0]["sql"] == CORRECTED_SQL
        # The APPROVER, not the mechanism: certified_by reaches trust_summary and
        # "approved by patch" names nobody. source: review:* records the mechanism.
        assert certified[0]["created_by"] == "token:t"
        assert (certified[0]["source"] or "").startswith("review:")

        cases = client.get("/mgmt/lenses/customer_value/evals/cases", headers=h).json()
        assert cases[0]["status"] == "candidate" and cases[0]["expected_sql"] == CORRECTED_SQL
    finally:
        _cleanup(org)


@needs_db
def test_certified_apply_without_embeddings_fails_and_patch_stays_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "Without embeddings" is this test's PREMISE — pin it, never inherit it
    # from the environment. Unpinned, it reads whatever DST_PROVIDERS holds:
    # with a local embedder installed one resolves, and the run fails 409
    # embedding_mismatch (dim 384 vs the columns' 1024) instead of the 503 this
    # test is about.
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="number",
            corrected_sql=CORRECTED_SQL,
            drafter_script="unused",
        )
        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert r.status_code == 503, r.text
        listed = client.get("/mgmt/lenses/customer_value/patches", headers=h).json()
        assert listed[0]["status"] == "candidate"  # nothing half-applied
    finally:
        _cleanup(org)


@needs_db
def test_approve_never_re_measures_the_lens_it_did_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve used to re-run the regression set and report the score as evidence the
    fix worked — scoring the OLD lens, since the fix had not landed. The measurement
    that counts is the eval gate on the apply that lands the file."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,
            drafter_script='{"body": "fixed body"}',
        )

        from services.evals import service as evals_service

        def never(**kwargs: object) -> object:
            raise AssertionError("approve must not run an eval — the fix has not landed yet")

        monkeypatch.setattr(evals_service, "build_and_run", never)
        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert "eval_run" not in out and "eval_detail" not in out
        assert client.get("/mgmt/lenses/customer_value/evals/runs", headers=h).json() == []
    finally:
        _cleanup(org)


@needs_db
def test_reject_patch_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,
            drafter_script='{"body": "should never land"}',
        )
        before = client.get("/mgmt/lenses/customer_value", headers=h).json()["draft"]
        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/reject", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        # the draft bundle is untouched and no eval case was created
        lens = client.get("/mgmt/lenses/customer_value", headers=h).json()
        defs = {d["term"]: d["body"] for d in lens["draft"]["semantic_model"]["definitions"]}
        assert defs["repeat_customer"] == ORIGINAL_BODY
        assert lens["draft"] == before
        assert client.get("/mgmt/lenses/customer_value/evals/cases", headers=h).json() == []
        missing = f"/mgmt/reviews/patches/{uuid.uuid4()}/reject"
        assert client.post(missing, headers=h).status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_approve_instruction_patch_proposes_the_lens_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="scope",
            corrected_sql=None,
            drafter_script='{"instruction": "Exclude internal accounts unless asked."}',
        )
        assert cand["kind"] == "instruction"
        assert cand["target"] == "customer_value"
        before = client.get("/mgmt/lenses/customer_value", headers=h).json()["draft"]
        r = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()

        # instructions are authored truth — proposed as a lens.yaml edit, never written
        assert out["applied"] == {}
        assert out["live"] is False
        proposed = out["proposed_file"]
        assert proposed["path"] == "lenses/customer_value/lens.yaml"
        instructions = yaml.safe_load(proposed["content"])["instructions"]
        assert "Exclude internal accounts" in instructions
        assert "+" in proposed["diff"]
        lens = client.get("/mgmt/lenses/customer_value", headers=h).json()
        assert lens["draft"] == before

        # an instruction patch has no verified oracle either — behavioral, not the wrong sql
        case = client.get("/mgmt/lenses/customer_value/evals/cases", headers=h).json()[0]
        assert case["expected_sql"] is None and case["expect"] == "answer"
    finally:
        _cleanup(org)


# ── the repro: a ruling lands only through its file ──────────────────────────


def _project_files() -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    return files


def _served_body(h: dict[str, str], term: str) -> str:
    lens = client.get("/mgmt/lenses/customer_value", headers=h).json()
    bundle = lens.get("published") or lens["draft"]
    return next(d["body"] for d in bundle["semantic_model"]["definitions"] if d["term"] == term)


@needs_db
def test_approved_definition_survives_the_next_apply_only_through_its_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this endpoint had: approve wrote the corrected body into the DB bundle,
    and re-applying the SAME unchanged files recompiled the lens and reverted it —
    silently, with no warning anywhere. Now the ruling comes back as a file: unchanged
    files change nothing, and landing the proposed file makes the correction stick
    across every later apply."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    new_body = "A repeat customer has 2+ orders in the last 12 months."
    try:
        with org_session(org) as s:  # type: ignore[arg-type]
            connection_store.create_connection(
                s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
            )
            s.commit()
        files = _project_files()
        out = client.post("/mgmt/project/apply", headers=h, json={"files": files}).json()
        assert next(e for e in out if e.get("lens") == "customer_value")["action"] == "created"

        cand = _setup_ticket_with_patch(
            h,
            org,
            monkeypatch,
            kind="definition",
            corrected_sql=None,
            drafter_script=f'{{"body": "{new_body}"}}',
            create_lens=False,  # this lens came from files, the way real ones do
        )
        approval = client.post(f"/mgmt/reviews/patches/{cand['id']}/approve", headers=h).json()
        assert approval["live"] is False
        proposed = approval["proposed_file"]

        # Re-apply the SAME files: the old approve had written the body into the
        # bundle and this recompile ate it. Nothing to eat now — and nothing lost,
        # because the ruling came back as a file instead of hiding in the DB.
        client.post("/mgmt/project/apply", headers=h, json={"files": files})
        assert _served_body(h, "repeat_customer") == ORIGINAL_BODY

        # Land the proposed file: the ruling takes effect, and keeps taking effect.
        files[proposed["path"]] = proposed["content"]
        out = client.post("/mgmt/project/apply", headers=h, json={"files": files}).json()
        assert not any(e.get("action") == "aborted" for e in out), out
        assert _served_body(h, "repeat_customer") == new_body
        client.post("/mgmt/project/apply", headers=h, json={"files": files})
        assert _served_body(h, "repeat_customer") == new_body

        # the eval case the ruling filed is governing state: applies never delete it
        cases = client.get("/mgmt/lenses/customer_value/evals/cases", headers=h).json()
        assert [c["id"] for c in cases] == [approval["eval_case_id"]]

        # The harvested case never carries the trace's WRONG sql as its oracle —
        # and the behavioral shape it carries instead round-trips
        # through export → apply → plan, so `dst plan` can go clean.
        exported = client.get("/mgmt/project/export", headers=h).json()["files"]
        cases_yaml = exported["lenses/customer_value/evals/cases.yaml"]
        assert TRACE_SQL not in cases_yaml
        assert yaml.safe_load(cases_yaml)[0]["expect"] == "answer"
        files["lenses/customer_value/evals/cases.yaml"] = cases_yaml
        out = client.post("/mgmt/project/apply", headers=h, json={"files": files}).json()
        assert not any(e.get("action") == "aborted" for e in out), out
        planned = client.post("/mgmt/project/plan", headers=h, json={"files": files}).json()
        lens_row = next(e for e in planned if e.get("lens") == "customer_value")
        dirty = [d["path"] for d in lens_row["diffs"] if d["path"].endswith("cases.yaml")]
        assert dirty == [], lens_row
    finally:
        _cleanup(org)
