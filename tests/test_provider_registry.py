"""Regression pins for the provider registry (genericized).

Providers are pure config — no vendor-named settings exist. These snapshots pin
the resolved (provider-kind, model) per path under each configuration, and the
architecture test pins the seam: provider clients are constructed inside
services/llm/ only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from services.api.llm import require_llm
from services.config import ProviderConfig, settings
from services.contracts.lens_config import ModelConfig
from services.llm import registry, resolve_provider
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.assist import assist_llm
from services.llm.retry import RetryingLLM

_ANTHROPIC = {"anthropic": ProviderConfig(type="anthropic", api_key="sk-a")}
# Declaration order is the cost policy: the cheap provider first.
_CHEAP_PLUS_ANTHROPIC = {
    "cheapco": ProviderConfig(
        type="openai-compatible",
        api_key="sk-d",
        base_url="https://api.provider.example",
        fast_model="cheap-flash",
    ),
    "anthropic": ProviderConfig(type="anthropic", api_key="sk-a"),
}
_SELF_HOSTED = {
    "ollama": ProviderConfig(
        type="openai-compatible",
        api_key="unused",
        base_url="http://localhost:11434/v1",
        models=["llama3"],
        smart_model="llama3",
    ),
}


def _use(monkeypatch: pytest.MonkeyPatch, providers: dict[str, ProviderConfig]) -> None:
    monkeypatch.setattr(settings, "providers", providers)


def _shape(resolved: registry.ResolvedModel | None) -> tuple[str, str] | None:
    if resolved is None:
        return None
    # resolve() wraps every client in RetryingLLM — unwrap to see the vendor.
    assert isinstance(resolved.llm, RetryingLLM)
    kind = "anthropic" if isinstance(resolved.llm.inner, AnthropicProvider) else "openai-compatible"
    return (kind, resolved.name)


# ── tier policy snapshots ─────────────────────────────────────────────────────


def test_fast_tier_is_first_declared_provider_with_a_fast_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _CHEAP_PLUS_ANTHROPIC)
    assert registry.tier("fast") == "cheapco/cheap-flash"
    _use(monkeypatch, _ANTHROPIC)
    assert registry.tier("fast") == "anthropic/claude-haiku-4-5"  # type default


def test_smart_tier_skips_providers_without_a_smart_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cheapco declares no smart model → smart falls through to anthropic's type default.
    _use(monkeypatch, _CHEAP_PLUS_ANTHROPIC)
    assert registry.tier("smart") == "anthropic/claude-sonnet-4-6"
    _use(monkeypatch, _SELF_HOSTED)
    assert registry.tier("smart") == "ollama/llama3"


def test_tiers_empty_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {})
    assert registry.tier("fast") == ""
    assert registry.resolve(registry.tier("fast")) is None


def test_fast_sibling_stays_on_the_lens_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _CHEAP_PLUS_ANTHROPIC)
    assert registry.fast_sibling("claude-sonnet-4-6") == "anthropic/claude-haiku-4-5"
    assert registry.fast_sibling("cheapco/cheap-pro") == "cheapco/cheap-flash"
    # No fast model on the provider → no cheap tier, the ref itself comes back.
    _use(monkeypatch, _SELF_HOSTED)
    assert registry.fast_sibling("ollama/llama3") == "ollama/llama3"


# ── resolution snapshots ──────────────────────────────────────────────────────


def test_bare_refs_resolve_via_catalog_then_type_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _CHEAP_PLUS_ANTHROPIC)
    # Catalog/tier-model match wins.
    assert _shape(registry.resolve("cheap-flash")) == ("openai-compatible", "cheap-flash")
    # Anthropic-type entries claim claude-* names.
    assert _shape(registry.resolve("claude-sonnet-4-6")) == ("anthropic", "claude-sonnet-4-6")
    # Unknown bare names fall back to the first declared provider.
    assert _shape(registry.resolve("mystery-model")) == ("openai-compatible", "mystery-model")


def test_prefixed_refs_pin_their_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _SELF_HOSTED)
    assert _shape(registry.resolve("ollama/llama3")) == ("openai-compatible", "llama3")
    assert registry.resolve("anthropic/claude-sonnet-4-6") is None  # entry not configured


def test_claude_names_unclaimed_without_an_anthropic_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BYOK trap: the ModelConfig default is claude-named, so an
    # ollama-only install must get "unresolved" (actionable 503 / publish
    # warning) — never claude silently posted to its endpoint as a query-time
    # "model not found".
    _use(monkeypatch, _SELF_HOSTED)
    assert registry.split_ref("claude-sonnet-4-6") == (None, "claude-sonnet-4-6")
    assert registry.resolve("claude-sonnet-4-6") is None
    # A gateway that really serves claude models catalogs them — catalog wins.
    gateway = {
        "gw": ProviderConfig(
            type="openai-compatible",
            api_key="sk-g",
            base_url="https://gw.example",
            models=["claude-sonnet-4-6"],
        ),
    }
    _use(monkeypatch, gateway)
    assert _shape(registry.resolve("claude-sonnet-4-6")) == (
        "openai-compatible",
        "claude-sonnet-4-6",
    )


def test_assist_runs_on_the_fast_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _CHEAP_PLUS_ANTHROPIC)
    assert _shape(assist_llm()) == ("openai-compatible", "cheap-flash")
    _use(monkeypatch, _ANTHROPIC)
    assert _shape(assist_llm()) == ("anthropic", "claude-haiku-4-5")
    _use(monkeypatch, {})
    assert assist_llm() is None
    assert resolve_provider("claude-sonnet-4-6") is None


# ── lens ModelConfig ref semantics ────────────────────────────────────────────


def test_model_ref_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset → "", which the registry reads as this install's own tier. It is NOT
    # a vendor default: that shipped a claude name to DeepSeek-only installs,
    # which published green and 503'd on every question (tests/test_byok_model_default.py).
    assert ModelConfig().model_ref() == ""
    _use(monkeypatch, _SELF_HOSTED)
    assert registry.split_ref(ModelConfig().model_ref()) == ("ollama", "llama3")
    assert ModelConfig(model="claude-sonnet-4-6").model_ref() == "claude-sonnet-4-6"  # bare
    assert ModelConfig(provider="ollama", model="llama3").model_ref() == "ollama/llama3"


# ── keyless degradation ───────────────────────────────────────────────────────


def test_require_llm_503_names_the_key_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(
        monkeypatch,
        {
            "anthropic": ProviderConfig(type="anthropic", api_key_env="MY_CLAUDE_KEY"),
        },
    )
    monkeypatch.delenv("MY_CLAUDE_KEY", raising=False)
    with pytest.raises(HTTPException) as ei:
        require_llm("claude-sonnet-4-6")
    assert ei.value.status_code == 503
    assert "MY_CLAUDE_KEY" in str(ei.value.detail)
    _use(monkeypatch, {})
    with pytest.raises(HTTPException) as ei:
        require_llm("claude-sonnet-4-6")
    assert "DST_PROVIDERS" in str(ei.value.detail)


def test_api_key_env_reads_the_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, {"anthropic": ProviderConfig(type="anthropic", api_key_env="MY_KEY")})
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    assert _shape(registry.resolve("claude-sonnet-4-6")) == ("anthropic", "claude-sonnet-4-6")


def test_router_decider_degrades_to_cosine_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.api.route import _resolve_decider

    _use(monkeypatch, {})
    assert _resolve_decider() is None


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ValueError):
        ProviderConfig(type="openai-compatible", api_key="k")


# ── architecture pin: construction stays behind the seam ──────────────────────

_CONSTRUCT = re.compile(r"\b(AnthropicProvider|OpenAICompatProvider|DeepSeekProvider)\(")
# The benchmark harness deliberately builds providers from raw env (decoupled from
# app settings); everything else goes through the registry.
_EXEMPT = ("services/llm/", "services/benchmark/")


def test_providers_are_constructed_only_in_services_llm() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for py in (root / "services").rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if rel.startswith(_EXEMPT):
            continue
        if _CONSTRUCT.search(py.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], (
        f"construct providers via services.llm.registry, not directly: {offenders}"
    )


def test_no_vendor_named_key_settings_exist() -> None:
    """The open core has no vendor's API key as a first-class setting."""
    field_names = set(type(settings).model_fields)
    assert not {"anthropic_api_key", "deepseek_api_key", "deepseek_fast_model"} & field_names


# ── embedder resolution ──────────────────────────────────────────────────────


def test_embedder_resolves_from_provider_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.context.openai_embedder import OpenAICompatEmbedder

    _use(
        monkeypatch,
        {
            "anthropic": ProviderConfig(type="anthropic", api_key="sk-a"),
            "local": ProviderConfig(
                type="openai-compatible",
                api_key="k",
                base_url="http://localhost:11434/v1",
                embedding_model="nomic-embed",
                embedding_dim=1024,
            ),
        },
    )
    embedder = registry.resolve_embedder()
    assert isinstance(embedder, OpenAICompatEmbedder) and embedder.dim == 1024


def test_embedder_none_when_no_provider_serves_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # …and the local-embed extra is absent: with it installed, an install with
    # no embedding provider falls back to the in-process tier.
    monkeypatch.setattr(registry, "find_spec", lambda name: None)
    _use(monkeypatch, _ANTHROPIC)
    assert registry.resolve_embedder() is None
    _use(monkeypatch, {})
    assert registry.resolve_embedder() is None


def test_any_positive_embedding_dim_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any positive dim is legal config — the write-path guard and
    `dst reindex` own the safety, not the parser."""
    from services.context.openai_embedder import OpenAICompatEmbedder

    cfg = ProviderConfig(
        type="openai-compatible",
        api_key="k",
        base_url="http://x",
        embedding_model="m",
        embedding_dim=768,
    )
    assert cfg.embedding_dim == 768
    _use(monkeypatch, {"local": cfg})
    embedder = registry.resolve_embedder()
    assert isinstance(embedder, OpenAICompatEmbedder) and embedder.dim == 768
    with pytest.raises(ValueError, match="positive"):
        ProviderConfig(type="openai-compatible", base_url="http://x", embedding_dim=0)


def test_no_vendor_named_key_settings_exist_including_voyage() -> None:
    assert "voyage_api_key" not in set(type(settings).model_fields)
