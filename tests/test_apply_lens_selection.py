"""`dst apply --lens` publishes a selection of the push, not the whole tree.

The push still carries every lens; the non-selected ones are treated as
unchanged, so a sibling whose gate is red no longer blocks the lens being
shipped. The invariant the selection may not break: no published lens is
left compiled against shared provenance that no longer matches. A push that
changes a shared entity a non-selected published lens reads is refused up
front, naming the lens and both ways out (select it too, or drop --lens).
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.contracts.lens_config import LensConfig
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import (
    jaffle_customer_value_bundle,
    jaffle_customer_value_config,
    jaffle_shared_assets,
)
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.semantic.files import render_semantic_files

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

A = "customer_value"
B = "order_activity"
_RED_CASE = [{"question": "How many orders were placed?", "expect": "answer"}]


@pytest.fixture()
def org():
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('ApplySel') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        s.commit()
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _lens_files(
    name: str, config: LensConfig, cases: list[dict[str, object]] | None
) -> dict[str, str]:
    files = {
        f"lenses/{name}/{path}": content
        for path, content in render_lens_repo(jaffle_customer_value_bundle()).items()
    }
    files[f"lenses/{name}/lens.yaml"] = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    files[f"lenses/{name}/queries.yaml"] = yaml.safe_dump(
        {"use_when": [], "sample_queries": []}, sort_keys=False, allow_unicode=True
    )
    files[f"lenses/{name}/certified_answers.yaml"] = yaml.safe_dump([], sort_keys=False)
    if cases is not None:
        files[f"lenses/{name}/evals/cases.yaml"] = yaml.safe_dump(cases, sort_keys=False)
    return files


def _files(
    *,
    b_cases: list[dict[str, object]] | None = None,
    a_note: str | None = None,
    orders_tweak: str | None = None,
) -> dict[str, str]:
    """Two lenses over the jaffle layer: A (customers + orders) and B (orders
    only, eval_gate: block). A's queries.yaml carries ``a_note`` so a push can
    change A alone; ``orders_tweak`` edits the shared orders entity B reads."""
    entities, definitions, relationships = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions, relationships))
    if orders_tweak:
        files["semantic/entities/orders.yaml"] = files["semantic/entities/orders.yaml"].replace(
            "One row per order.", orders_tweak
        )
    a = jaffle_customer_value_config()
    a.eval_gate = "block"
    files.update(_lens_files(A, a, None))
    if a_note:
        files[f"lenses/{A}/queries.yaml"] = yaml.safe_dump(
            {"use_when": [a_note], "sample_queries": []}, sort_keys=False
        )
    b = jaffle_customer_value_config()
    b.name, b.display_name = B, "Order Activity"
    b.description = "Order counts over the jaffle dataset."
    b.select.entities = [e for e in b.select.entities if e.name == "orders"]
    b.select.definitions = []
    b.eval_gate = "block"
    files.update(_lens_files(B, b, b_cases))
    return files


def _apply(headers: dict[str, str], files: dict[str, str], lens: list[str] | None = None):  # type: ignore[no-untyped-def]
    params = [("lens", name) for name in lens] if lens else None
    return client.post("/mgmt/project/apply", headers=headers, params=params, json={"files": files})


def _garbage_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation that never produces SQL: a must-answer case under it is red."""
    from services.evals import runner as _runner

    monkeypatch.setattr(_runner, "GATE_ATTEMPTS", 1)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM(["GARBAGE ;;;"] * 8), "fake", "m"),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)


@needs_db
def test_a_selected_lens_publishes_past_a_siblings_red_gate(org, monkeypatch) -> None:
    _oid, headers = org
    _garbage_llm(monkeypatch)
    out = _apply(headers, _files()).json()
    assert {e.get("lens"): e.get("action") for e in out if e.get("lens")} == {
        A: "created",
        B: "created",
    }

    # B now carries a case its gate cannot pass; a whole-tree apply aborts on it.
    _garbage_llm(monkeypatch)
    out = _apply(headers, _files(b_cases=_RED_CASE, a_note="customer questions")).json()
    assert next(e for e in out if e.get("lens") == B)["action"] == "rejected"
    assert out[-1] == {
        "scope": "apply",
        "action": "aborted",
        "detail": "nothing deployed — fix the errors and re-apply",
    }

    # Selecting A ships A; B is unchanged, red case and all.
    _garbage_llm(monkeypatch)
    r = _apply(headers, _files(b_cases=_RED_CASE, a_note="customer questions"), lens=[A])
    assert r.status_code == 200, r.text
    out = r.json()
    assert not any(e.get("errors") for e in out), out
    assert not any(e.get("action") == "aborted" for e in out)
    rows = {e["lens"]: e for e in out if e.get("lens")}
    assert rows[A]["action"] == "updated" and rows[A]["version"] is not None
    assert rows[B]["action"] == "unchanged"


@needs_db
def test_a_selection_that_would_leave_a_sibling_stale_is_refused_naming_it(
    org, monkeypatch
) -> None:
    _oid, headers = org
    _garbage_llm(monkeypatch)
    assert not any(e.get("errors") for e in _apply(headers, _files()).json())

    # The orders entity changes; B reads it and is not selected.
    r = _apply(headers, _files(orders_tweak="One row per placed order."), lens=[A])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert B in detail and "entity/orders" in detail
    assert "add them to --lens" in detail and "drop --lens" in detail
    # Nothing landed: the shared layer the push carried rolled back with the refusal.
    with org_session(_oid) as s:
        from services.semantic import store as semantic_store

        orders = next(
            a for a in semantic_store.list_assets(s, "entity") if a.body["name"] == "orders"
        )
        assert "placed order" not in str(orders.body)

    # Selecting both is the first way out; the whole tree is the other.
    _garbage_llm(monkeypatch)
    r = _apply(headers, _files(orders_tweak="One row per placed order."), lens=[A, B])
    assert r.status_code == 200, r.text
    rows = {e["lens"]: e["action"] for e in r.json() if e.get("lens")}
    assert rows == {A: "updated", B: "updated"}


@needs_db
def test_a_selected_lens_must_be_in_the_push(org) -> None:
    _oid, headers = org
    r = _apply(headers, _files(), lens=["nope"])
    assert r.status_code == 422 and "nope" in r.json()["detail"]
