"""The LLM coverage decider + cosine-shortlist composition.

Division of labor: cosine shortlists (recall the candidate lenses), the decider
decides (it reads coverage where cosine over-declines paraphrases at scale).
Control flow pinned here with a FAKE provider (no network): the ONLY
non-LLM route is the near-verbatim confident fast-path; the deleted floor/margin
arm must never resurrect; decider outage (error, starved reply) DECLINES with
DECIDER_DOWN — never a silent fallback to the mid-band cosine route."""

from __future__ import annotations

import pytest

from services.contracts.fakes import HashEmbedder
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.router import DECIDER_DOWN, CoverageProfile, Router
from services.router.decider import DeciderStarved, LlmDecider, _parse, route_with_llm, shortlist_k

PROFILES = [
    CoverageProfile(lens="finance", anchors=["total net invoiced revenue"], description="invoices"),
    CoverageProfile(lens="sales", anchors=["quote win rate"], description="deals"),
]
BY_LENS = {p.lens: p for p in PROFILES}


class _FakeProvider:
    """Returns a canned completion, counts calls, and keeps the last prompt — so
    the tests can assert the LLM is (not) hit and what shortlist it saw."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.last_user = ""

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.calls += 1
        self.last_user = messages[-1].content
        return LLMResult(text=self.reply)


def _router() -> Router:
    return Router(PROFILES, HashEmbedder())


def test_llm_pick_routes_when_cosine_is_unsure() -> None:
    # a paraphrase (not a verbatim anchor) lands below the confident bar → the LLM decides
    provider = _FakeProvider("finance")
    d = route_with_llm(_router(), BY_LENS, LlmDecider(provider, "m"), "how much have we billed")
    assert d.covered and d.lens == "finance"
    assert d.reason == "routed (llm)"
    assert provider.calls == 1  # the LLM was consulted


def test_llm_none_is_an_honest_decline() -> None:
    provider = _FakeProvider("none")
    d = route_with_llm(_router(), BY_LENS, LlmDecider(provider, "m"), "how many support tickets")
    assert not d.covered and d.lens is None
    assert "no governed lens covers this" in d.reason
    assert d.degraded is None  # a genuine decline, not an outage
    assert provider.calls == 1


def test_confident_cosine_match_fast_paths_without_the_llm() -> None:
    # a verbatim anchor → cosine 1.0 ≥ confident → route immediately, no LLM call (cost saver)
    provider = _FakeProvider("sales")  # would mis-route if it were (wrongly) consulted
    d = route_with_llm(_router(), BY_LENS, LlmDecider(provider, "m"), "total net invoiced revenue")
    assert d.covered and d.lens == "finance"
    assert d.reason == "routed (cosine confident)"
    assert provider.calls == 0  # the fast path skipped the LLM entirely


def test_no_lenses_is_a_clean_decline() -> None:
    empty = Router([], HashEmbedder())
    d = route_with_llm(empty, {}, LlmDecider(_FakeProvider("finance"), "m"), "anything")
    assert not d.covered and d.lens is None


def test_parse_is_robust_to_formatting() -> None:
    names = {"finance", "customer_value", "sales"}
    assert _parse("finance", names) == "finance"
    assert _parse("**customer_value**", names) == "customer_value"
    assert _parse("the customer value lens", names) == "customer_value"  # space form
    assert _parse("It is sales.", names) == "sales"
    assert _parse("none", names) is None
    assert _parse("", names) is None


class _StubRouter:
    """Controls scored()/thresholds so route_with_llm's gating is tested directly —
    HashEmbedder can only make verbatim=1.0 vs ~random, with no tunable mid-band."""

    def __init__(self, scored: list[tuple[str, float]]) -> None:
        self._scored = scored
        self.threshold = 0.78
        self.confident = 0.95
        self.margin = 0.07

    def scored(self, question: str) -> list[tuple[str, float]]:
        return list(self._scored)


_STUB_BY_LENS = {
    "board": CoverageProfile(lens="board", anchors=["board kpis"], description="board reporting"),
    "sales": CoverageProfile(lens="sales", anchors=["commission"], description="commissions"),
}


def test_mid_band_with_wide_margin_still_consults_the_decider() -> None:
    # The floor/margin arm is DELETED as a decision path. 0.86 with a
    # 0.16 gap routed on cosine alone before — the weakest routing arm.
    # Now everything below `confident` is the decider's call.
    provider = _FakeProvider("board")
    d = route_with_llm(
        _StubRouter([("board", 0.86), ("sales", 0.70)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(provider, "m"),
        "board numbers",
    )
    assert d.covered and d.lens == "board"
    assert d.reason == "routed (llm)"  # never "routed (cosine)" from the mid-band
    assert provider.calls == 1


def test_tied_cosine_falls_to_the_llm() -> None:
    # Two lenses ~tied, and cosine even ranks the wrong one first — only the
    # LLM can break the tie correctly.
    provider = _FakeProvider("board")
    d = route_with_llm(
        _StubRouter([("sales", 0.807), ("board", 0.801)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(provider, "m"),
        "pull the latest numbers for the board meeting",
    )
    assert d.covered and d.lens == "board"
    assert provider.calls == 1  # the tie-break needed the LLM


def test_decider_error_declines_degraded_never_mid_band_routes() -> None:
    # 0.86/0.70 would have routed on the deleted arm — an outage must
    # produce a LOUD decline, never resurrect that route as a fallback.
    class _Boom:
        def complete(self, **kwargs: object) -> LLMResult:
            raise RuntimeError("provider down")

    d = route_with_llm(
        _StubRouter([("board", 0.86), ("sales", 0.70)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(_Boom(), "m"),  # type: ignore[arg-type]
        "board numbers",
    )
    assert not d.covered and d.lens is None
    assert d.degraded == DECIDER_DOWN
    assert "declining" in d.reason


def test_starved_empty_reply_is_an_outage_not_a_decline() -> None:
    # max-tokens starvation returns empty — treating it as 'none' silently
    # declined everything. It must read as an outage.
    provider = _FakeProvider("")
    decider = LlmDecider(provider, "m")
    with pytest.raises(DeciderStarved):
        decider.decide("board numbers", list(_STUB_BY_LENS.values()))
    d = route_with_llm(
        _StubRouter([("board", 0.86), ("sales", 0.70)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(_FakeProvider(""), "m"),
        "board numbers",
    )
    assert not d.covered and d.degraded == DECIDER_DOWN


def test_garbage_reply_is_an_outage_too() -> None:
    d = route_with_llm(
        _StubRouter([("board", 0.86), ("sales", 0.70)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(_FakeProvider("today I reasoned about weather"), "m"),
        "board numbers",
    )
    assert not d.covered and d.degraded == DECIDER_DOWN


def test_none_inside_a_sentence_still_declines_honestly() -> None:
    d = route_with_llm(
        _StubRouter([("board", 0.86), ("sales", 0.70)]),  # type: ignore[arg-type]
        _STUB_BY_LENS,
        LlmDecider(_FakeProvider("None of these lenses fit."), "m"),
        "how many open tickets",
    )
    assert not d.covered and d.degraded is None
    assert "no governed lens covers this" in d.reason


def test_shortlist_scales_with_the_catalogue() -> None:
    # frozen k=5 silently truncated recall as catalogues grew.
    assert shortlist_k(2) == 5
    assert shortlist_k(10) == 5
    assert shortlist_k(12) == 6
    assert shortlist_k(20) == 10
    assert shortlist_k(40) == 12  # prompt-budget ceiling


def test_dynamic_shortlist_reaches_the_decider_prompt() -> None:
    # 12 lenses → shortlist_k = 6 candidate lines in the catalogue block.
    lenses = [
        CoverageProfile(lens=f"lens_{i:02d}", anchors=[f"metric {i}"], description="")
        for i in range(12)
    ]
    scored = [(p.lens, 0.90 - i * 0.01) for i, p in enumerate(lenses)]
    provider = _FakeProvider("lens_03")
    d = route_with_llm(
        _StubRouter(scored),  # type: ignore[arg-type]
        {p.lens: p for p in lenses},
        LlmDecider(provider, "m"),
        "some mid-band question",
    )
    assert d.covered and d.lens == "lens_03"
    catalogue_lines = [ln for ln in provider.last_user.splitlines() if ln.startswith("- ")]
    assert len(catalogue_lines) == shortlist_k(12)
