"""The store half: certify-time prose — composed once, kept until re-authored.

A fixed certified answer gets its English written ONCE from the executed result
(`apply --probe-certified` composes over the probe's rows; `rule --certify`
snapshots the served answer the human just approved) and round-trips through
certified_answers.yaml like verified_value. Every miss degrades to no-prose —
keyless installs, templates, probe failures — never to a failed apply; a SQL
re-author CLEARS the stored prose (it described the old SQL's result).
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import HashEmbedder, ScriptedLLM
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.runtime.answer import AnswerComposer
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


def _vec(i: int) -> list[float]:
    v = [0.0] * 1024
    v[i] = 1.0
    return v


@pytest.fixture()
def org(monkeypatch):
    from services.project import apply as apply_engine

    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    # Keyless by default (the degrade case); tests that want a certify-time
    # composer re-patch resolve. The self-test is not under test here.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    monkeypatch.setattr(apply_engine, "_certify_self_test", lambda *a, **k: 0)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertProseT') RETURNING id")
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
            "request_log",
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"
_PROSE = "Repeat customers hold steady this period."


def _files(answers: list[dict[str, object]]) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    return files


def _apply(
    headers: dict[str, str], files: dict[str, str], probe: bool = False
) -> dict[str, object]:
    out = client.post(
        "/mgmt/project/apply",
        headers=headers,
        params={"probe_certified": "true"} if probe else {},
        json={"files": files},
    ).json()
    return next(e for e in out if e.get("lens") == "customer_value")


def _stored(oid: object) -> certify_store.CertifiedAnswer:
    with org_session(oid) as s:
        return next(
            a
            for a in certify_store.list_for_lens(s, "customer_value")
            if a.question.strip().lower() == _Q.strip().lower()
        )


# ─── store round-trip ────────────────────────────────────────────────────────


@needs_db
def test_store_round_trips_verified_prose(org) -> None:
    oid, _headers = org
    with org_session(oid) as s:
        cid = certify_store.create(
            s, "customer_value", _Q, _SQL, _vec(0), "apply", verified_prose=_PROSE
        )
    with org_session(oid) as s:
        row = certify_store.get(s, cid)
        assert row is not None and row.verified_prose == _PROSE
        # Search — the SERVE path's read — carries it too, or the short-circuit
        # could never fire on a matched answer.
        hits = certify_store.search(s, "customer_value", _vec(0), k=1)
        assert hits and hits[0].answer.verified_prose == _PROSE
    with org_session(oid) as s:
        # None keeps (an update that says nothing about prose must not erase it)…
        certify_store.update(s, cid, sql=_SQL, verified_value={"value": 19})
        assert certify_store.get(s, cid).verified_prose == _PROSE  # type: ignore[union-attr]
        # …clear_verified_prose is the explicit erase (SQL re-author).
        certify_store.update(s, cid, sql=_SQL + " ", clear_verified_prose=True)
        assert certify_store.get(s, cid).verified_prose is None  # type: ignore[union-attr]


# ─── apply --probe-certified composes once ───────────────────────────────────


@needs_db
def test_probe_composes_prose_once_and_stores_it(org, monkeypatch) -> None:
    oid, headers = org
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([_PROSE]), "fake", "m"),
    )
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}]), probe=True)
    assert not row.get("errors")
    stored = _stored(oid)
    assert stored.verified_prose == _PROSE
    assert stored.verified_value is not None  # the probe still records the value


@needs_db
def test_probe_keyless_degrades_to_no_prose_and_green_apply(org) -> None:
    # The fixture pins registry.resolve → None: no composer exists, the apply
    # must land the answer prose-less rather than fail (test-env doctrine).
    oid, headers = org
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}]), probe=True)
    assert not row.get("errors")
    assert _stored(oid).verified_prose is None
    assert _stored(oid).verified_value is not None  # probe itself still ran


@needs_db
def test_template_answers_never_store_prose(org, monkeypatch) -> None:
    # A template's serve renders deterministically per binding — one sample's
    # prose must never speak for every binding.
    oid, headers = org
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([_PROSE]), "fake", "m"),
    )
    template = {
        "question": "How many customers with more than {n} orders?",
        "sql": "SELECT count(*) FROM customers WHERE number_of_orders > {n}",
        "slots": {"n": {"type": "number"}},
        "sample_bindings": [{"n": "1"}],
    }
    row = _apply(headers, _files([template]), probe=True)
    assert not row.get("errors")
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
    assert stored.slots and stored.verified_prose is None


@needs_db
def test_grounding_fail_refuses_to_freeze_prose(org, monkeypatch) -> None:
    # Prose stating a figure the result does not hold must never
    # become the verbatim serve — certify-time compose degrades to no-prose.
    oid, headers = org
    ungrounded = ScriptedLLM(["Exactly 123456789 repeat customers."])
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ungrounded, "fake", "m"),
    )
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}]), probe=True)
    assert not row.get("errors")
    assert _stored(oid).verified_prose is None


# ─── file round-trip: files win ──────────────────────────────────────────────


@needs_db
def test_file_round_trip_and_sql_edit_clears(org) -> None:
    oid, headers = org
    # A file can carry the prose itself (an exported tree re-applied).
    entry: dict[str, object] = {"question": _Q, "sql": _SQL, "verified_prose": _PROSE}
    assert not _apply(headers, _files([entry])).get("errors")
    assert _stored(oid).verified_prose == _PROSE

    # Re-apply with the key OMITTED: not an edit — the stored prose survives
    # (the verified_value doctrine).
    assert not _apply(headers, _files([{"question": _Q, "sql": _SQL}])).get("errors")
    assert _stored(oid).verified_prose == _PROSE

    # An explicit new value is an edit; files win.
    entry["verified_prose"] = "A different approved sentence."
    assert not _apply(headers, _files([entry])).get("errors")
    assert _stored(oid).verified_prose == "A different approved sentence."

    # A SQL re-author clears prose composed from the OLD SQL's result.
    edited = {"question": _Q, "sql": _SQL + " AND number_of_orders < 100"}
    assert not _apply(headers, _files([edited])).get("errors")
    stored = _stored(oid)
    assert stored.sql.endswith("< 100") and stored.verified_prose is None


@needs_db
def test_export_renders_verified_prose(org) -> None:
    _oid, _headers = org
    bundle = jaffle_customer_value_bundle()
    answer = certify_store.CertifiedAnswer(
        id="c1",
        lens="customer_value",
        question=_Q,
        sql=_SQL,
        created_by="apply",
        created_at="2026-08-18T00:00:00+00:00",
        verified_prose=_PROSE,
    )
    files = render_lens_repo(bundle, certified_answers=[answer])
    pairs = yaml.safe_load(files["certified_answers.yaml"])
    assert pairs[0]["verified_prose"] == _PROSE
    # Optional key: an answer without prose renders without it (pre-T.1 files
    # stay byte-identical).
    bare = certify_store.CertifiedAnswer(**{**answer.__dict__, "verified_prose": None})
    files = render_lens_repo(bundle, certified_answers=[bare])
    assert "verified_prose" not in yaml.safe_load(files["certified_answers.yaml"])[0]


# ─── rule --certify snapshots the approved answer ────────────────────────────


@needs_db
def test_certify_from_request_stores_served_prose(org, monkeypatch) -> None:
    oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, row_count, answer, definition_used, confidence, certification, "
                "latency, status) VALUES "
                "(:o, 'req-prose-1', 'customer_value', 'agent-1', :q, :s, true, 1, "
                ":a, 'repeat_customer', 'verified', 'none', '{}'::jsonb, 'ok')"
            ),
            {"o": oid, "q": _Q, "s": _SQL, "a": "There are 19 repeat customers."},
        )
    r = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-prose-1", headers=headers
    )
    assert r.status_code == 201, r.text
    with org_session(oid) as s:
        stored = certify_store.get(s, r.json()["id"])
    assert stored is not None
    assert stored.verified_prose == "There are 19 repeat customers."


@needs_db
def test_certify_from_request_without_confidence_stores_no_prose(org, monkeypatch) -> None:
    # No graded confidence = the trace's answer is not prose over real rows
    # (an error line, a refusal) — never freeze it.
    oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, "
                "valid, answer, certification, latency, status) VALUES "
                "(:o, 'req-prose-2', 'customer_value', 'agent-1', :q, :s, false, "
                "'The query could not be executed: boom', 'none', '{}'::jsonb, 'error')"
            ),
            {"o": oid, "q": _Q, "s": _SQL},
        )
    r = client.post(
        "/mgmt/lenses/customer_value/certified/from-request/req-prose-2", headers=headers
    )
    assert r.status_code == 201, r.text
    with org_session(oid) as s:
        stored = certify_store.get(s, r.json()["id"])
    assert stored is not None and stored.verified_prose is None


# ─── the serve short-circuit (no DB: pipeline over the jaffle duckdb file) ───


class _MustNotCompose(AnswerComposer):
    """A composer whose call is the failure: the certified short-circuit exists
    so this is never reached."""

    def __init__(self) -> None:
        super().__init__(ScriptedLLM(["should never be written"]))

    def compose(self, **_kw: object):  # type: ignore[override]
        raise AssertionError("composer called on a deterministic certified serve")


def _serve(**overrides: object):
    from services.connectors.duckdb import DuckDBConnector
    from services.lenses.demo import jaffle_customer_value
    from services.runtime.generator import FixedSQLGenerator
    from services.runtime.pipeline import run_query

    kwargs: dict[str, object] = dict(
        question="how many repeat customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        generator=FixedSQLGenerator(_SQL),
        composer=_MustNotCompose(),
        certification="certified",
        certified_match="exact",
    )
    kwargs.update(overrides)
    return run_query(**kwargs)  # type: ignore[arg-type]


def test_stored_prose_serves_verbatim_and_short_circuits_composer() -> None:
    res = _serve(verified_prose=_PROSE)
    assert res.response.answer == _PROSE  # verbatim — the composer never ran
    assert res.response.certification == "certified"
    assert res.response.confidence == "verified"  # the static certified state
    # numeric_grounding SKIPPED by name — never run over verbatim prose.
    assert res.response.verification is not None
    check = res.response.verification.check("numeric_grounding")
    assert check is not None and check.status == "skip"
    assert check.reason is not None and "verbatim" in check.reason
    # SQL and data survive exactly as today (the guard normalizes case).
    assert res.response.sql is not None and res.response.sql.lower() == _SQL.lower()
    assert res.response.data is not None and res.response.data.rows
    # Latency/cost proof: ZERO AI spend — no LLM call anywhere on the path.
    assert res.trace.ai_input_tokens == 0 and res.trace.ai_output_tokens == 0
    assert res.trace.ai_cost_usd == 0.0
    assert "compose" not in res.trace.latency_ms


def test_stored_prose_is_byte_identical_across_serves() -> None:
    served = [_serve(verified_prose=_PROSE) for _ in range(5)]
    answers = {r.response.answer for r in served}
    confidences = {r.response.confidence for r in served}
    assert answers == {_PROSE} and len(confidences) == 1


def test_legacy_certified_answer_without_prose_composes_as_before() -> None:
    from services.runtime.answer import AnswerComposer as RealComposer

    res = _serve(verified_prose=None, composer=RealComposer(ScriptedLLM(["Some repeat buyers."])))
    assert res.response.answer.startswith("Some repeat buyers.")
    assert res.trace.ai_input_tokens == 1  # exactly the one compose call


def test_template_serve_renders_deterministic_frame() -> None:
    question = "How many customers with more than 1 orders?"
    res = _serve(question=question, certified_match="parameterized")
    # The frame: question restated + the result as compact table lines — code,
    # not a model; the composer would have raised.
    assert res.response.answer.startswith(f"{question} — 1 result row:")
    assert res.trace.ai_input_tokens == 0 and res.trace.ai_cost_usd == 0.0
    assert res.response.confidence == "verified"
    check = res.response.verification.check("numeric_grounding")  # type: ignore[union-attr]
    assert check is not None and check.status == "skip"
    # Deterministic: two serves, one text.
    again = _serve(question=question, certified_match="parameterized")
    assert again.response.answer == res.response.answer


def test_certified_frame_formats_declared_money_2dp() -> None:
    from services.contracts.warehouse import QueryResult
    from services.lenses.demo import jaffle_customer_value
    from services.runtime.answer import certified_frame

    model = jaffle_customer_value()  # `revenue` declares format: currency
    result = QueryResult(
        columns=["order_date", "revenue"],
        rows=[["2026-07-01", 64517.170000000006], ["2026-08-01", 1234.5], ["2026-08-02", None]],
    )
    frame = certified_frame("revenue by day?", result, model)
    assert "64517.17" in frame and "64517.170000000006" not in frame  # noise trimmed…
    assert "1234.50" in frame  # …and money always shows cents (F-7, to the cent)
    assert "NULL" in frame
    # The undeclared column renders as stored.
    assert "2026-07-01" in frame


def test_frame_caps_rows_and_says_so() -> None:
    from services.contracts.warehouse import QueryResult
    from services.lenses.demo import jaffle_customer_value
    from services.runtime.answer import certified_frame

    result = QueryResult(columns=["n"], rows=[[i] for i in range(10)])
    frame = certified_frame("numbers?", result, jaffle_customer_value(), max_rows=3)
    assert "(showing the first 3 of 10 rows)" in frame
    assert "\n9" not in frame
