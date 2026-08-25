"""The patch drafter — a correction becomes the smallest concrete,
approvable fix. Drafting is pure (ScriptedLLM, no DB); persistence + the mgmt surface
round-trip through patch_store and the API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.correction import CorrectionDelta
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.profile import TableProfile
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.contracts.semantic_model import Definition
from services.lenses.demo import jaffle_customer_value_bundle
from services.lenses.profile_store import StoredProfile
from services.lenses.store import LensBundle
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.reviews.patch import PatchCandidate, draft_patch
from services.reviews.store import Ticket, Trace

client = TestClient(app)

# ─── fixtures ────────────────────────────────────────────────────────────────


class RecordingLLM(ScriptedLLM):
    """ScriptedLLM that keeps the user prompts it was sent (last one is `prompt`)."""

    def __init__(self, responses: list[str] | None = None) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.prompts.append("\n".join(m.content for m in messages))
        return super().complete(
            system=system,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def _trace(**overrides: object) -> Trace:
    base: dict[str, object] = {
        "request_id": "req-1",
        "lens": "customer_value",
        "caller": "agent-1",
        "question": "how many repeat customers?",
        "sql": "SELECT count(*) FROM customers WHERE number_of_orders > 1",
        "answer": "There are 19 repeat customers.",
        "definition_used": "repeat_customer",
        "confidence": "high",
        "row_count": 1,
    }
    base.update(overrides)
    return Trace.model_validate(base)


def _ticket(correction: CorrectionDelta | None) -> Ticket:
    return Ticket(
        ticket_id="rev_test1",
        request_id="req-1",
        lens="customer_value",
        caller="agent-1",
        state="needs_human",
        correction=correction,
    )


def _stored_profile(table: str, *, days_old: int) -> StoredProfile:
    when = datetime.now(UTC) - timedelta(days=days_old)
    return StoredProfile(
        profile=TableProfile(connection="jaffle", table=table, last_updated_logical=when),
        previous=None,
        profiled_at=when,
    )


# ─── routing: definition ─────────────────────────────────────────────────────


def test_definition_correction_drafts_definition_patch_with_diff() -> None:
    llm = ScriptedLLM(['{"body": "A repeat customer has 2+ orders in the last 12 months."}'])
    correction = CorrectionDelta(
        kind="definition", note="repeat_customer must be windowed to the last 12 months"
    )
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None
    assert out.kind == "definition"
    assert out.target == "repeat_customer"
    assert out.owner == "lens-owner"
    assert out.diff_before == "A repeat customer has number_of_orders > 1."
    assert out.diff_after == "A repeat customer has 2+ orders in the last 12 months."
    assert out.status == "candidate"
    assert out.ticket_id == "rev_test1"


def test_definition_patch_falls_back_to_note_on_bad_llm_json() -> None:
    """Unparseable draft → the note itself, AMENDED onto the body. The fallback used
    to return the bare note, which replaced the whole definition with it."""
    llm = ScriptedLLM(["not json"])
    correction = CorrectionDelta(kind="definition", note="repeat_customer needs a time window")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert out.diff_after == (
        "A repeat customer has number_of_orders > 1.\n\nrepeat_customer needs a time window"
    )


def test_unknown_definition_targets_the_traced_definition() -> None:
    """A note that names no known term still lands on the definition the trace used."""
    llm = ScriptedLLM(['{"body": "fixed"}'])
    correction = CorrectionDelta(kind="definition", note="the threshold is wrong")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.target == "repeat_customer"


# ─── routing: explicit target ────────────────────────────────────────────────


def test_explicit_target_overrides_note_vocabulary() -> None:
    """Bag-of-words routing sent cross-cutting notes onto unrelated shared
    definitions. An explicit target is authoritative — the note stops steering."""
    amended = (
        "Lifetime value is a customer's total realized order revenue in USD - the sum of "
        "their order amounts to date, surfaced as customers.customer_lifetime_value. "
        "Realized value only: never projected or modeled forward, and never including "
        "open pipeline. Note that returned orders still count toward it - the jaffle "
        "mart does not net them out."
    )
    llm = ScriptedLLM(['{"body": "' + amended + '"}'])
    correction = CorrectionDelta(
        kind="definition",
        # Vocabulary dominated by another term — note routing would pick repeat_customer.
        note="repeat_customer is fine; the lifetime figure must stay realized-only",
        target="lifetime_value",
    )
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert out.target == "lifetime_value"
    assert out.diff_before is not None and out.diff_before.startswith("Lifetime value")
    assert out.diff_after == amended  # a real amendment replaces the ruling in place


def test_explicit_target_naming_a_new_term_drafts_a_new_definition() -> None:
    """The failing case: 'create a NEW definition … do NOT modify X' still landed on X.
    An explicit target no definition carries must draft a NEW one under that term,
    verbatim — beating both the note's vocabulary and the traced definition."""
    llm = ScriptedLLM(['{"body": "Growth rate compares consecutive closing balances."}'])
    correction = CorrectionDelta(
        kind="definition",
        note="create a NEW definition — do NOT modify repeat_customer",
        target="closing balance growth rate",
    )
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert out.target == "closing balance growth rate"
    assert out.diff_before is None  # new term: no existing body was touched
    assert out.diff_after == "Growth rate compares consecutive closing balances."


def test_absent_target_keeps_note_routing() -> None:
    """No target → today's behavior: the note's vocabulary picks the definition."""
    llm = ScriptedLLM(['{"body": "fixed"}'])
    correction = CorrectionDelta(kind="definition", note="lifetime_value is projected wrong")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.target == "lifetime_value"


# ─── routing: target beats kind ──────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["scope", "number", "freshness", "other"])
def test_explicit_target_outranks_every_kind(kind: str) -> None:
    """`{kind: "scope", target: "<an existing definition>"}` drafted a lens
    instruction; the identical prose as kind: "definition" targeted correctly.
    The curator named a term — that IS the routing decision, whatever box the
    correction was filed under. `kind` only decides when nothing is named."""
    llm = ScriptedLLM(['{"body": "Lifetime value is realized order revenue only."}'])
    correction = CorrectionDelta(
        kind=kind,  # type: ignore[arg-type]
        note="projected revenue is being included",
        target="lifetime_value",
    )
    profiles = [_stored_profile("orders", days_old=30)]  # would route freshness→reprofile
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), profiles)
    assert out is not None
    assert out.kind == "definition", f"kind={kind} with an explicit target must patch the term"
    assert out.target == "lifetime_value"
    assert out.diff_before is not None and out.diff_before.startswith("Lifetime value")


def test_target_naming_an_unknown_term_under_a_non_definition_kind_drafts_it() -> None:
    """Same rule for a term the lens doesn't carry yet: the named term is drafted
    as a NEW definition, not diverted into a lens instruction."""
    llm = ScriptedLLM(['{"body": "Overtake scope counts on-track passes only."}'])
    correction = CorrectionDelta(
        kind="scope", note="pit-stop position swaps were counted", target="overtake_scope"
    )
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert out.target == "overtake_scope"
    assert out.diff_before is None


def test_target_yields_to_corrected_sql() -> None:
    """The one thing that still outranks a target: corrected SQL. A definition's
    prose does not change generated SQL — the certified pair does."""
    correction = CorrectionDelta(
        kind="scope",
        note="wrong filter",
        target="lifetime_value",
        corrected_sql="SELECT count(*) FROM customers WHERE number_of_orders >= 2",
    )
    out = draft_patch(None, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "certified"


def test_no_target_still_routes_scope_to_an_instruction() -> None:
    """The regression guard on the other side: without a target, kind still rules."""
    llm = ScriptedLLM(['{"instruction": "Exclude internal accounts unless asked."}'])
    correction = CorrectionDelta(kind="scope", note="internal accounts were included", target="  ")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "instruction"


# ─── amend, never replace ────────────────────────────────────────────────────

# Four rulings in one definition: the meaning, the SQL formula, a dismissal rule,
# and a pointer at a sibling term. Draft 1 kept ONE and dropped the other three.
FOUR_RULINGS = (
    "The month-over-month growth of a customer's closing balance, defined ONLY over "
    "the customer's own active months.\n\n"
    "growth rate = (current month closing balance - prior month closing balance) / "
    "prior month closing balance * 100.\n\n"
    "Pit-stop position swaps are NOT growth events and must be dismissed before the "
    "series is built.\n\n"
    "For the balance itself, see [[month-end balance]] - its global-sequence padding "
    "rule governs reporting, not growth rates."
)
LOSSY = "Closing balance growth is the month-over-month change in a customer's balance."


def _four_ruling_bundle() -> LensBundle:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(term="closing_balance_growth_rate", body=FOUR_RULINGS)
    )
    return bundle


def test_lossy_draft_cannot_delete_rulings_the_note_never_mentioned() -> None:
    """A four-ruling definition came back as one sentence: the SQL formula, a
    load-bearing dismissal rule and a sibling-term pointer silently gone. The
    drafter AMENDS — a draft may rewrite any ruling and add any ruling, but a
    ruling the correction never mentioned cannot vanish."""
    llm = ScriptedLLM(['{"body": "' + LOSSY + '"}'])
    correction = CorrectionDelta(
        kind="definition",
        note="growth is only defined over the customer's own active months",
        target="closing_balance_growth_rate",
    )
    out = draft_patch(llm, _ticket(correction), _trace(), _four_ruling_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert "growth rate = (current month closing balance" in out.diff_after  # the formula
    assert "Pit-stop position swaps are NOT growth events" in out.diff_after  # the dismissal
    assert "[[month-end balance]]" in out.diff_after  # the sibling pointer
    assert LOSSY in out.diff_after  # …and the drafter's own sentence still lands


def test_redraft_after_rejection_still_retains_the_unmentioned_rulings() -> None:
    """The other direction: the rejection `--note` named exactly what to
    preserve and the SECOND draft was lossier still. A redraft is bound by the same
    amendment rule as the first — the note cannot be used to shrink the term."""
    rejected = PatchCandidate(
        id="p1",
        ticket_id="rev_test1",
        lens="customer_value",
        kind="definition",
        target="closing_balance_growth_rate",
        diff_after=LOSSY,
        status="rejected",
        rejection_note="you dropped the SQL formula and the pit-stop dismissal rule - keep them",
    )
    llm = ScriptedLLM(['{"body": "Growth compares consecutive closing balances."}'])
    correction = CorrectionDelta(
        kind="definition", note="use active months only", target="closing_balance_growth_rate"
    )
    out = draft_patch(
        llm, _ticket(correction), _trace(), _four_ruling_bundle(), [], rejections=[rejected]
    )
    assert out is not None
    assert "growth rate = (current month closing balance" in out.diff_after
    assert "Pit-stop position swaps are NOT growth events" in out.diff_after
    assert "[[month-end balance]]" in out.diff_after


def test_rejection_notes_reach_the_redraft_prompt() -> None:
    """`rejection_note` was stored on the candidate and consumed by NOTHING, so the
    dst-flywheel skill's promise that it is drafter feedback was false. Every
    prior rejection, and its reason, must be in front of the model that redrafts."""
    llm = RecordingLLM(['{"body": "' + FOUR_RULINGS.replace("\n", " ") + '"}'])
    rejected = [
        PatchCandidate(
            id="p1",
            ticket_id="rev_test1",
            lens="customer_value",
            kind="definition",
            target="closing_balance_growth_rate",
            diff_after="first bad draft",
            status="rejected",
            rejection_note="keep the SQL formula",
        ),
        PatchCandidate(
            id="p2",
            ticket_id="rev_test1",
            lens="customer_value",
            kind="definition",
            target="closing_balance_growth_rate",
            diff_after="second bad draft",
            status="rejected",
            rejection_note="and the pit-stop dismissal rule",
        ),
    ]
    correction = CorrectionDelta(
        kind="definition", note="active months only", target="closing_balance_growth_rate"
    )
    draft_patch(llm, _ticket(correction), _trace(), _four_ruling_bundle(), [], rejections=rejected)
    prompt = llm.prompts[-1]
    assert "keep the SQL formula" in prompt
    assert "and the pit-stop dismissal rule" in prompt
    assert "first bad draft" in prompt and "second bad draft" in prompt
    assert "REJECTED" in prompt  # named as constraints, not buried as trivia


def test_rejection_notes_reach_the_instruction_redraft_too() -> None:
    llm = RecordingLLM(['{"instruction": "Exclude internal accounts unless asked."}'])
    rejected = PatchCandidate(
        id="p1",
        ticket_id="rev_test1",
        lens="customer_value",
        kind="instruction",
        target="customer_value",
        diff_after="Exclude everything internal.",
        status="rejected",
        rejection_note="too broad - only internal ACCOUNTS, not internal orders",
    )
    correction = CorrectionDelta(kind="scope", note="internal accounts were included")
    out = draft_patch(
        llm,
        _ticket(correction),
        _trace(),
        jaffle_customer_value_bundle(),
        [],
        rejections=[rejected],
    )
    assert out is not None and out.kind == "instruction"
    assert "only internal ACCOUNTS" in llm.prompts[-1]


def test_a_new_term_is_not_amended_onto_anything() -> None:
    """No existing body, nothing to preserve — the draft stands alone."""
    llm = ScriptedLLM(['{"body": "Overtake scope counts on-track passes only."}'])
    correction = CorrectionDelta(kind="definition", note="pit swaps counted", target="overtake_x")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None
    assert out.diff_before is None
    assert out.diff_after == "Overtake scope counts on-track passes only."


# ─── routing: certified ──────────────────────────────────────────────────────


def test_corrected_sql_drafts_certified_candidate() -> None:
    correction = CorrectionDelta(
        kind="number",
        note="the count is off by one",
        corrected_sql="SELECT count(*) FROM customers WHERE number_of_orders >= 2",
    )
    out = draft_patch(None, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None
    assert out.kind == "certified"
    assert out.target == "how many repeat customers?"
    assert out.diff_before == "SELECT count(*) FROM customers WHERE number_of_orders > 1"
    assert out.diff_after.endswith(">= 2")


def test_certified_tier_trace_routes_to_certified_even_for_definition_kind() -> None:
    """When FixedSQL served the answer, only fixing the stored pair changes anything."""
    correction = CorrectionDelta(kind="definition", note="wrong pair", corrected_sql="SELECT 2")
    trace = _trace(certification="certified")
    out = draft_patch(None, _ticket(correction), trace, jaffle_customer_value_bundle(), [])
    assert out is not None and out.kind == "certified"
    assert out.diff_after == "SELECT 2"


# ─── routing: freshness ──────────────────────────────────────────────────────


def test_freshness_with_stalled_profile_drafts_reprofile() -> None:
    correction = CorrectionDelta(kind="freshness", note="numbers look a week old")
    profiles = [_stored_profile("customers", days_old=1), _stored_profile("orders", days_old=30)]
    out = draft_patch(None, _ticket(correction), _trace(), jaffle_customer_value_bundle(), profiles)
    assert out is not None
    assert out.kind == "reprofile"
    assert out.target == "orders"
    assert "orders" in out.diff_after


def test_freshness_without_stalled_profile_drafts_reindex() -> None:
    correction = CorrectionDelta(kind="freshness", note="docs feel stale")
    profiles = [_stored_profile("customers", days_old=1)]
    out = draft_patch(None, _ticket(correction), _trace(), jaffle_customer_value_bundle(), profiles)
    assert out is not None
    assert out.kind == "reindex"
    assert out.target == "jaffle"


# ─── routing: instruction + nothing ──────────────────────────────────────────


def test_scope_note_drafts_lens_instruction() -> None:
    llm = ScriptedLLM(['{"instruction": "Exclude internal accounts unless asked."}'])
    correction = CorrectionDelta(kind="scope", note="internal accounts were included")
    out = draft_patch(llm, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is not None
    assert out.kind == "instruction"
    assert out.target == "customer_value"  # the instruction lands on the lens itself
    assert out.diff_after == "Exclude internal accounts unless asked."


def test_no_correction_drafts_nothing() -> None:
    out = draft_patch(None, _ticket(None), _trace(), jaffle_customer_value_bundle(), [])
    assert out is None


def test_empty_correction_drafts_nothing() -> None:
    correction = CorrectionDelta(kind="other", note="   ")
    out = draft_patch(None, _ticket(correction), _trace(), jaffle_customer_value_bundle(), [])
    assert out is None


# ─── persistence + API round-trip (needs Postgres) ───────────────────────────


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
        org = c.execute(text("INSERT INTO org (name) VALUES ('PatchT') RETURNING id")).scalar_one()
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
        c.execute(text("DELETE FROM patch_candidate WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM review WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_patch_store_round_trip() -> None:
    from services.db.session import org_session
    from services.reviews import patch_store

    org, _ = _org_token()
    cand = PatchCandidate(
        ticket_id=None,  # ticket-less (the distiller path) must persist fine
        lens="customer_value",
        kind="certified",
        target="how many repeat customers?",
        diff_before="SELECT 1",
        diff_after="SELECT 2",
    )
    try:
        with org_session(org) as s:  # type: ignore[arg-type]
            pid = patch_store.record(s, cand)
            got = patch_store.get(s, pid)
            assert got is not None and got.kind == "certified" and got.ticket_id is None
            assert [c.id for c in patch_store.list_for_lens(s, "customer_value")] == [pid]
            assert patch_store.approve(s, pid) == 1
            assert patch_store.get(s, pid).status == "approved"  # type: ignore[union-attr]
            # status flips only out of 'candidate' — approve/reject are one-shot
            assert patch_store.reject(s, pid) == 0
            assert patch_store.list_for_lens(s, "customer_value", status="approved")[0].id == pid
            # Rejection stores the reason — drafter feedback, not a dead end
            pid2 = patch_store.record(s, cand.model_copy(update={"diff_after": "SELECT 3"}))
            assert patch_store.reject(s, pid2, "mistargeted — belongs on lifetime_value") == 1
            got2 = patch_store.get(s, pid2)
            assert got2 is not None and got2.status == "rejected"
            assert got2.rejection_note == "mistargeted — belongs on lifetime_value"
            assert patch_store.list_for_lens(s, "customer_value", status="rejected")[0].id == pid2
    finally:
        _cleanup(org)


@needs_db
def test_draft_patch_endpoint_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correction-carrying ticket → POST draft-patch → approvable diff in the lens list."""
    org, raw = _org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # One shared script: the judge (review open) consumes the first response, the
    # drafter the second — a fresh ScriptedLLM per construction would replay the judge.
    llm = ScriptedLLM(
        [
            '{"verdict": "changes", "reasoning": "definition disputed"}',  # judge
            '{"body": "A repeat customer has 2+ orders in the last 12 months."}',  # drafter
        ]
    )
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: llm)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda ref: ResolvedModel(llm, "fake", registry.wire_name(ref)),
    )
    try:
        body = jaffle_customer_value_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=body, headers=h).status_code == 201

        ticket = client.post(
            "/v1/reviews",
            headers=h,
            json={
                "request_id": rid,
                "correction": {"kind": "definition", "note": "repeat_customer needs a window"},
            },
        ).json()
        tid = ticket["ticket_id"]

        drafted = client.post(f"/mgmt/reviews/{tid}/draft-patch", headers=h)
        assert drafted.status_code == 201, drafted.text
        cand = drafted.json()
        assert cand["kind"] == "definition"
        assert cand["target"] == "repeat_customer"
        assert cand["diff_before"] == "A repeat customer has number_of_orders > 1."
        assert cand["diff_after"] == "A repeat customer has 2+ orders in the last 12 months."
        assert cand["status"] == "candidate"
        assert cand["id"]

        listed = client.get("/mgmt/lenses/customer_value/patches", headers=h)
        assert listed.status_code == 200
        assert [p["id"] for p in listed.json()] == [cand["id"]]
    finally:
        _cleanup(org)


@needs_db
def test_rejecting_a_draft_feeds_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the store: reject draft 1 with a note
    naming what to keep, redraft — the note reaches the model AND the rulings the
    correction never mentioned are still there. Draft 2 used to be lossier still."""
    org, raw = _org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    llm = RecordingLLM(
        [
            '{"verdict": "changes", "reasoning": "definition disputed"}',  # judge
            '{"body": "' + LOSSY + '"}',  # draft 1: drops three of four rulings
            '{"body": "Growth compares consecutive closing balances."}',  # draft 2: lossier
        ]
    )
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: llm)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda ref: ResolvedModel(llm, "fake", registry.wire_name(ref)),
    )
    note = "you dropped the SQL formula and the pit-stop dismissal rule - keep them"
    try:
        body = _four_ruling_bundle().model_dump(mode="json")
        assert client.post("/mgmt/lenses", json=body, headers=h).status_code == 201
        tid = client.post(
            "/v1/reviews",
            headers=h,
            json={
                "request_id": rid,
                "correction": {
                    "kind": "definition",
                    "note": "growth is only defined over the customer's own active months",
                    "target": "closing_balance_growth_rate",
                },
            },
        ).json()["ticket_id"]

        first = client.post(f"/mgmt/reviews/{tid}/draft-patch", headers=h).json()
        assert (
            client.post(
                f"/mgmt/reviews/patches/{first['id']}/reject", headers=h, json={"note": note}
            ).status_code
            == 200
        )

        second = client.post(f"/mgmt/reviews/{tid}/draft-patch", headers=h)
        assert second.status_code == 201, second.text
        redraft = second.json()
        assert note in llm.prompts[-1]  # the reviewer's reason reached the redraft
        assert "growth rate = (current month closing balance" in redraft["diff_after"]
        assert "Pit-stop position swaps are NOT growth events" in redraft["diff_after"]
        assert "[[month-end balance]]" in redraft["diff_after"]
    finally:
        _cleanup(org)


@needs_db
def test_draft_patch_requires_a_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_trace(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "changes", "reasoning": "hm"}']),
    )
    try:
        tid = client.post("/v1/reviews", headers=h, json={"request_id": rid}).json()["ticket_id"]
        r = client.post(f"/mgmt/reviews/{tid}/draft-patch", headers=h)
        assert r.status_code == 400
        assert "no correction" in r.json()["detail"]
        assert client.post("/mgmt/reviews/nope/draft-patch", headers=h).status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_reject_patch_endpoint_stores_the_note() -> None:
    """The queue had approve-or-limbo — rejecting now lands the status AND the
    reason (drafter feedback), stays one-shot, and still takes a body-less call."""
    from services.db.session import org_session
    from services.reviews import patch_store

    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    cand = PatchCandidate(
        ticket_id=None,
        lens="customer_value",
        kind="definition",
        target="net transaction amount",
        diff_after="blanket no-padding rule",
    )
    try:
        with org_session(org) as s:  # type: ignore[arg-type]
            pid = patch_store.record(s, cand)
            pid2 = patch_store.record(s, cand.model_copy(update={"diff_after": "v2"}))
        note = "mistargeted — the correction is about series construction"
        r = client.post(f"/mgmt/reviews/patches/{pid}/reject", headers=h, json={"note": note})
        assert r.status_code == 200, r.text
        assert r.json() == {"id": pid, "status": "rejected"}
        with org_session(org) as s:  # type: ignore[arg-type]
            got = patch_store.get(s, pid)
            assert got is not None and got.status == "rejected" and got.rejection_note == note
        # one-shot: re-rejecting a ruled patch is a 404, and the note stays put
        assert client.post(f"/mgmt/reviews/patches/{pid}/reject", headers=h).status_code == 404
        # note-less body (the dashboard posts {}) keeps working; no note recorded
        r2 = client.post(f"/mgmt/reviews/patches/{pid2}/reject", headers=h, json={})
        assert r2.status_code == 200
        with org_session(org) as s:  # type: ignore[arg-type]
            got2 = patch_store.get(s, pid2)
            assert got2 is not None and got2.status == "rejected" and got2.rejection_note is None
    finally:
        _cleanup(org)


# ─── rulings written as SENTENCES, not paragraphs ────────────────────────────

# The half the paragraph guard never covered. Half of the real
# definition pages are a SINGLE paragraph carrying three to five rulings
# separated by full stops, and there paragraph matching protects nothing: one
# drafted paragraph scoring over the floor carries every sentence out with it.
TWO_RULINGS_ONE_PARAGRAPH = (
    "`fleet_volume` is the count of rows in the fact table. "
    "It exists so the probe has a governed term."
)


def _one_paragraph_bundle() -> LensBundle:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(term="fleet_volume", body=TWO_RULINGS_ONE_PARAGRAPH)
    )
    return bundle


def test_a_ruling_in_the_same_paragraph_is_not_collateral() -> None:
    """The drafted body scored 0.519 against the whole paragraph — just over the
    0.5 paragraph floor — so it counted as "that ruling, amended" and replaced
    BOTH rulings. The correction named one; the other was never mentioned and
    must survive."""
    note = (
        "fleet_volume must count rows in the fact table, not distinct keys. "
        "Keep the existing grain ruling and the existing join ruling."
    )
    llm = ScriptedLLM(['{"body": "' + note + '"}'])
    correction = CorrectionDelta(kind="definition", note=note, target="fleet_volume")
    out = draft_patch(llm, _ticket(correction), _trace(), _one_paragraph_bundle(), [])
    assert out is not None and out.kind == "definition"
    assert "It exists so the probe has a governed term." in out.diff_after  # untouched ruling
    assert "not distinct keys" in out.diff_after  # the correction, folded in
    # …and the ruling the note DID correct was rewritten in place, not duplicated.
    assert "is the count of rows in the fact table" not in out.diff_after


def test_the_llm_less_fallback_cannot_flatten_a_one_paragraph_body_either() -> None:
    """The note itself is the fallback body. Against a single paragraph of
    rulings it used to become the entire definition."""
    out = draft_patch(
        None,
        _ticket(
            CorrectionDelta(kind="definition", note="count rows, not keys", target="fleet_volume")
        ),
        _trace(),
        _one_paragraph_bundle(),
        [],
    )
    assert out is not None
    for sentence in ("count of rows in the fact table", "governed term"):
        assert sentence in out.diff_after


def test_sentence_splitting_does_not_break_a_dotted_identifier() -> None:
    """`customers.customer_lifetime_value` is ONE identifier. Splitting on a bare
    '.' invents rulings nobody wrote, and then restores them as duplicates."""
    from services.reviews.patch import _sentences

    body = "Lifetime value is surfaced as customers.customer_lifetime_value. Realized value only."
    assert _sentences(body) == [
        "Lifetime value is surfaced as customers.customer_lifetime_value.",
        "Realized value only.",
    ]
