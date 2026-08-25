"""The router experiment becomes a CI gate.

The held-out question set (scripts/router_experiment.py) runs offline in CI
against RECORDED embeddings (tests/fixtures/router_experiment_bge.json.gz, the
local bge-small tier — the zero-config default embedder). Deterministic, no
keys. Guards:

  * shortlist recall@k stays total — the recall device may never drop the gold
    lens from what the decider sees;
  * the confident fast-path never routes uncovered noise on its own;
  * the deleted floor/margin arm stays deleted (mid-band always consults the
    decider);
  * the labels stay held-out (contamination guard).

The decider-accuracy arm needs a live LLM: opt-in via DST_TEST_ROUTER_LIVE=1
(same pattern as the live auth e2e), run it before touching router internals,
thresholds, or shortlist_k — then update LAST_LIVE_RUN below.

bge-small is a production arm, not a stand-in fake.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from services.router import DEFAULT_CONFIDENT, CoverageProfile, Router
from services.router.decider import LlmDecider, route_with_llm, shortlist_k
from services.router.eval import assert_eval_held_out

# The model and embedder the LIVE decider arm was last verified against. Update on
# every DST_TEST_ROUTER_LIVE=1 run — a threshold/shortlist change without a
# fresh entry here is visible in review, which is the point.
LAST_LIVE_RUN = "deepseek-v4-flash (bge-small shortlist)"

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "router_experiment_bge.json.gz"


def _experiment():  # noqa: ANN202 — module type
    spec = importlib.util.spec_from_file_location(
        "router_experiment", _ROOT / "scripts" / "router_experiment.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("router_experiment", mod)
    spec.loader.exec_module(mod)
    return mod


class FixtureEmbedder:
    """Recorded bge-small vectors; an unrecorded text KeyErrors — the gate must
    fail loud if the labeled set and the fixture drift apart."""

    dim = 384

    def __init__(self) -> None:
        self._vecs: dict[str, list[float]] = json.loads(gzip.decompress(_FIXTURE.read_bytes()))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vecs[t] for t in texts]


@pytest.fixture(scope="module")
def exp():  # noqa: ANN201
    return _experiment()


@pytest.fixture(scope="module")
def router(exp) -> Router:  # noqa: ANN001
    return Router(exp.PROFILES, FixtureEmbedder())


def test_labels_stay_held_out(exp) -> None:  # noqa: ANN001
    assert_eval_held_out(exp.LABELED, exp.PROFILES)


def test_shortlist_recall_stays_total(exp, router: Router) -> None:  # noqa: ANN001
    """The recall device's one job: the gold lens is ALWAYS in the top-k the
    decider sees. Total at recording time — any drop is a regression."""
    k = shortlist_k(len(exp.PROFILES))
    misses = []
    for question, gold in exp.LABELED:
        if gold is None:
            continue
        top = [lens for lens, _ in router.scored(question)[:k]]
        if gold not in top:
            misses.append((question, gold, top))
    assert not misses, f"shortlist recall dropped: {misses}"


def test_confident_fast_path_never_routes_noise(exp, router: Router) -> None:  # noqa: ANN001
    """Uncovered questions must stay below the confident bar — the only non-LLM
    route left. If compression pushes noise above it, the fast-path
    itself has become a mis-router and the bar must move."""
    for question, gold in exp.LABELED:
        if gold is not None:
            continue
        scored = router.scored(question)
        assert scored and scored[0][1] < DEFAULT_CONFIDENT, (
            f"uncovered {question!r} reaches the confident fast-path ({scored[0]})"
        )


class _GoldDecider(LlmDecider):
    """Answers with the gold label (routing correctness is the live lane's job —
    offline we pin CONTROL FLOW: who gets consulted, what reasons appear)."""

    def __init__(self, gold: str | None) -> None:  # no provider
        self._gold = gold
        self.calls = 0

    def decide(self, question: str, candidates: list[CoverageProfile]) -> str | None:
        self.calls += 1
        return self._gold if any(p.lens == self._gold for p in candidates) else None


def test_deleted_floor_margin_arm_stays_deleted(exp, router: Router) -> None:  # noqa: ANN001
    """Every question below the confident bar consults the decider — the old
    'routed (cosine)' mid-band reason must never resurrect."""
    by_lens = {p.lens: p for p in exp.PROFILES}
    consulted = 0
    for question, gold in exp.LABELED:
        decider = _GoldDecider(gold)
        decision = route_with_llm(router, by_lens, decider, question)
        assert decision.reason != "routed (cosine)", question
        if decision.covered:
            assert decision.reason in {"routed (cosine confident)", "routed (llm)"}
        consulted += decider.calls
    fast_pathed = sum(
        1
        for q, _ in exp.LABELED
        if router.scored(q) and router.scored(q)[0][1] >= DEFAULT_CONFIDENT
    )
    assert consulted == len(exp.LABELED) - fast_pathed


@pytest.mark.skipif(
    not os.environ.get("DST_TEST_ROUTER_LIVE"),
    reason="live decider lane: set DST_TEST_ROUTER_LIVE=1 + DST_TEST_ROUTER_LIVE_URL/KEY/MODEL",
)
def test_live_decider_accuracy(exp, router: Router) -> None:  # noqa: ANN001
    """The decider-accuracy arm, against a real fast-tier model. Run before
    touching router internals or thresholds; update LAST_LIVE_RUN with the result.

    Credentials ride DST_TEST_* vars (the Snowflake-lane pattern) because the
    conftest deliberately scrubs DST_PROVIDERS — the suite is provider-free by
    contract, and this lane must not depend on what the scrub removes."""
    from services.llm.openai_compat import OpenAICompatProvider

    base_url = os.environ.get("DST_TEST_ROUTER_LIVE_URL")
    api_key = os.environ.get("DST_TEST_ROUTER_LIVE_KEY")
    model = os.environ.get("DST_TEST_ROUTER_LIVE_MODEL", "deepseek-v4-flash")
    if not base_url or not api_key:
        pytest.skip("DST_TEST_ROUTER_LIVE_URL / _KEY not set")
    decider = LlmDecider(OpenAICompatProvider(api_key, base_url), model)
    by_lens = {p.lens: p for p in exp.PROFILES}
    correct = 0
    for question, gold in exp.LABELED:
        decision = route_with_llm(router, by_lens, decider, question)
        got = decision.lens if decision.covered else None
        correct += got == gold
    accuracy = correct / len(exp.LABELED)
    print(f"\nlive decider accuracy: {correct}/{len(exp.LABELED)} = {accuracy:.0%}")
    assert accuracy >= 0.90, f"decider accuracy regressed: {accuracy:.0%}"
