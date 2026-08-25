"""The BYOK invariant: a lens that names no model must be SERVABLE.

The bug class: a lens with no `model:` block defaults to a literal model name,
`dst apply` publishes it green on an install whose providers cannot serve that
name, and every question comes back 503 — apply succeeds loudly and the failure
lands later, somewhere else.

Two guarantees are pinned here, and they are complementary:

1. **Resolution** — an unnamed model follows the install's own tier, so a lens
   authored on any BYOK install answers on that install.
2. **The gate** — when a lens's model genuinely cannot be served, `apply` must
   REFUSE it (validate error), naming what it tried and what is configured.

Together they make "green apply, 503 on every question" unreachable: either the
lens resolves, or it never publishes.
"""

from __future__ import annotations

import pytest

from services.config import ProviderConfig, settings
from services.contracts.lens_config import LensConfig, ModelConfig
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.llm import registry
from services.runtime.assembly import AssembledInputs, select_generators
from services.validate.report import validate_bundle

# A single-provider install: DeepSeek for generation, in-process embeddings,
# no Anthropic entry anywhere.
DEEPSEEK_ONLY = {
    "deepseek": ProviderConfig(
        type="openai-compatible",
        api_key="sk-d",
        base_url="https://api.deepseek.com",
        fast_model="deepseek-v4-flash",
        smart_model="deepseek-v4-pro",
    ),
}
OLLAMA_ONLY = {
    "ollama": ProviderConfig(
        type="openai-compatible",
        api_key="unused",
        base_url="http://localhost:11434/v1",
        smart_model="llama3",
    ),
}
FAST_ONLY = {
    "cheapco": ProviderConfig(
        type="openai-compatible",
        api_key="sk-c",
        base_url="https://api.provider.example",
        fast_model="cheap-flash",
    ),
}
# The mirror image, where the cross-provider misroute shows up: a server that
# has only Anthropic, serving a lens that pins DeepSeek.
ANTHROPIC_ONLY = {"anthropic": ProviderConfig(type="anthropic", api_key="sk-ant")}


def _use(monkeypatch: pytest.MonkeyPatch, providers: dict[str, ProviderConfig]) -> None:
    monkeypatch.setattr(settings, "providers", providers)


def _bundle(model: ModelConfig | None = None) -> LensBundle:
    """A minimal lens: one entity, one count metric, no `model:` block."""
    config = LensConfig(
        name="sales",
        display_name="Sales",
        connections=["warehouse"],
        **({"model": model} if model is not None else {}),
    )
    sm = SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="warehouse", table="main.orders"),
                fields=[Field(name="id", type="integer")],
                metrics=[Metric(name="order_count", type="simple", agg="count")],
            )
        ],
    )
    return LensBundle(config=config, semantic_model=sm)


# ── 1. resolution: no `model:` block follows the install ─────────────────────


def test_unnamed_model_serves_on_a_non_anthropic_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression pin. Author a lens with no `model:` block on a
    DeepSeek-only install; it must resolve to a client, not to a vendor the
    install never configured."""
    _use(monkeypatch, DEEPSEEK_ONLY)
    ref = _bundle().config.model.model_ref()
    assert registry.resolve(ref) is not None, "a lens with no model: block cannot answer"
    assert registry.split_ref(ref) == ("deepseek", "deepseek-v4-pro")
    # …and the cheap first pass stays on the same account.
    assert registry.fast_sibling(ref) == "deepseek/deepseek-v4-flash"
    # A provider with no fast model collapses the cheap tier onto the same
    # model — the serving path splits the sibling ref again, so both halves of
    # the tiering must survive the empty ref (services/runtime/assembly.py).
    _use(monkeypatch, OLLAMA_ONLY)
    assert registry.split_ref(registry.fast_sibling(ref)) == ("ollama", "llama3")
    _use(monkeypatch, DEEPSEEK_ONLY)
    # Never the old hard-coded default.
    assert registry.wire_name(ref) != "claude-sonnet-4-6"


def test_unnamed_model_follows_whatever_the_install_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, OLLAMA_ONLY)
    assert registry.split_ref(_bundle().config.model.model_ref()) == ("ollama", "llama3")
    # No smart model anywhere → the fast tier, because a cheaper answer beats
    # no answer at all.
    _use(monkeypatch, FAST_ONLY)
    assert registry.split_ref(_bundle().config.model.model_ref()) == ("cheapco", "cheap-flash")
    # An anthropic-type install still gets its type defaults — the provider-name
    # fallbacks above never displace them.
    _use(monkeypatch, {"anthropic": ProviderConfig(type="anthropic", api_key="sk-a")})
    assert registry.split_ref(_bundle().config.model.model_ref()) == (
        "anthropic",
        "claude-sonnet-4-6",
    )


def test_an_explicitly_named_model_is_still_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means "follow the install"; NAMED means named. Authors who pin a
    model keep it — the default changed, the override did not."""
    _use(monkeypatch, DEEPSEEK_ONLY)
    bundle = _bundle(ModelConfig(model="deepseek-v4-flash"))
    assert registry.split_ref(bundle.config.model.model_ref()) == ("deepseek", "deepseek-v4-flash")
    pinned = _bundle(ModelConfig(provider="deepseek", model="deepseek-chat"))
    assert registry.split_ref(pinned.config.model.model_ref()) == ("deepseek", "deepseek-chat")


def test_pre_byok_bundles_still_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundles stored before this change carry `provider: anthropic` in their
    JSON. They must keep resolving on an anthropic install exactly as before."""
    _use(monkeypatch, {"anthropic": ProviderConfig(type="anthropic", api_key="sk-a")})
    stored = ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    assert registry.split_ref(stored.model_ref()) == ("anthropic", "claude-sonnet-4-6")


# ── 2. the gate: apply refuses a lens that cannot answer ─────────────────────


def test_apply_publishes_the_unnamed_lens_on_a_deepseek_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, DEEPSEEK_ONLY)
    report = validate_bundle(_bundle(), [], [])
    assert report.ok, [i.message for i in report.issues if i.severity == "error"]
    assert not [i for i in report.issues if i.code == "lens_model_unservable"]


def test_apply_refuses_a_lens_whose_model_no_provider_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fix this was a WARNING and the lens published green. It is the
    gate now: publish fails, and the message names the model, the configured
    providers, and what this install would have served instead."""
    _use(monkeypatch, DEEPSEEK_ONLY)
    report = validate_bundle(_bundle(ModelConfig(model="claude-sonnet-4-6")), [], [])
    assert not report.ok
    issue = next(i for i in report.issues if i.code == "lens_model_unservable")
    assert issue.severity == "error"
    assert "claude-sonnet-4-6" in issue.message  # what it tried
    assert "deepseek" in issue.message  # what is configured
    assert "smart=deepseek/deepseek-v4-pro" in issue.message  # what it would serve


def test_apply_refuses_a_keyless_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared but keyless is the other half of unservable — and the message
    must name the env var that fixes it, not just the symptom."""
    _use(
        monkeypatch,
        {"anthropic": ProviderConfig(type="anthropic", api_key_env="MY_CLAUDE_KEY")},
    )
    monkeypatch.delenv("MY_CLAUDE_KEY", raising=False)
    report = validate_bundle(_bundle(), [], [])
    assert not report.ok
    issue = next(i for i in report.issues if i.code == "lens_model_unservable")
    assert "MY_CLAUDE_KEY" in issue.message


def test_a_wholly_unconfigured_install_warns_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No providers at all is a different disease: that install answers nothing
    at all (and /ready says so), and a keyless CI apply is a real workflow. Warn
    — the silent case is providers configured and THIS lens the odd one out."""
    _use(monkeypatch, {})
    report = validate_bundle(_bundle(), [], [])
    assert report.ok
    issue = next(i for i in report.issues if i.code == "lens_model_unservable")
    assert issue.severity == "warning"


# ── 3. the report the operator reads ─────────────────────────────────────────


def test_a_tier_is_a_ref_and_the_wire_wants_its_model_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tier()` returns 'provider/model'; the wire wants 'model'. Sending the ref
    404s on EVERY install, Anthropic included — the drift audit did exactly that
    (services/api/mgmt_audit.py) and its standing path swallowed the error, so
    scheduled audits produced fallback names forever. The name always comes from
    the pair that produced the client, which is what ``wire_name`` returns."""
    _use(monkeypatch, DEEPSEEK_ONLY)
    ref = registry.tier("fast")
    assert ref == "deepseek/deepseek-v4-flash"
    assert registry.wire_name(ref) == "deepseek-v4-flash"
    resolved = registry.resolve(ref)
    assert resolved is not None and resolved.name == registry.wire_name(ref)


def test_serving_summary_names_tier_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/ready`'s `models` line and the publish error share one wording —
    "which model does my lens run on?" had no answer anywhere before."""
    _use(monkeypatch, DEEPSEEK_ONLY)
    summary = registry.serving_summary()
    assert "fast=deepseek/deepseek-v4-flash" in summary
    assert "smart=deepseek/deepseek-v4-pro" in summary
    _use(monkeypatch, {})
    assert "none" in registry.serving_summary()


# ── 4. a pinned model NEVER routes to another vendor ─────────────────────────


class _Recorder:
    """A client that records the wire model name of every call instead of sending
    it. Standing in for the vendor that must NOT be dialled."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def complete(self, *, model: str, **_kw: object) -> object:
        self.models.append(model)
        raise AssertionError(f"unreachable in these tests (model={model!r})")


def test_a_pinned_model_no_provider_serves_never_reaches_another_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug class: a lens pinned to `deepseek/deepseek-v4-pro`, applied on an
    ANTHROPIC-ONLY server, sends a real request to Anthropic naming
    `deepseek-v4-pro` (HTTP 404 not_found_error). The client comes from apply's
    tier fallback; the NAME comes from `config.model.model_ref()` in
    `select_generators`. For a BYOK product that is its own category of defect —
    the operator's question, and whatever data rides in that prompt, goes to a
    vendor they did not choose and are not paying, and the diagnostic they get
    back is about a bad model name rather than a missing provider.

    Both halves are pinned. (a) There is no client to misroute with: the pinned
    ref does not resolve, and no fallback manufactures one. (b) Even handed a
    client, the generators name it from the resolution
    that produced it — the lens config supplies no model name at all."""
    _use(monkeypatch, ANTHROPIC_ONLY)
    pinned = ModelConfig(provider="deepseek", model="deepseek-v4-pro")
    ref = pinned.model_ref()

    # (a) nothing resolves — and the refusal names the model tried, the
    # configured providers, and what the tiers resolve to (8ab4099's contract).
    assert registry.resolve(ref) is None
    detail = registry.unservable_detail(ref)
    assert detail is not None
    assert "deepseek/deepseek-v4-pro" in detail  # what it tried
    assert "configured providers: anthropic" in detail  # what IS configured
    assert "smart=anthropic/claude-sonnet-4-6" in detail  # what would have served

    # (b) the generators take their wire name from the RESOLVED model, never
    # from the lens's `model:` block. This is the tier client the fallback used
    # to hand over — with the config still pinning deepseek.
    recorder = _Recorder()
    tier = registry.resolve(registry.tier("smart"))
    assert tier is not None
    resolved = registry.ResolvedModel(recorder, tier.provider, tier.name)
    assembled = AssembledInputs(
        model=_bundle().semantic_model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
    )
    generator, escalate, _mode, _band = select_generators(
        assembled, resolved, config=_bundle(pinned).config
    )
    # Both tiers live on the client's OWN provider: its fast sibling first, the
    # resolved model to escalate. Before the fix both said `deepseek-v4-pro` —
    # the pin leaked through the name AND through the sibling lookup, because
    # `fast_sibling` of an unconfigured provider returns the ref itself.
    assert escalate is not None
    assert (generator.model, escalate.model) == ("claude-haiku-4-5", "claude-sonnet-4-6")

    # …and prove it on the wire, not just on the attribute.
    with pytest.raises(AssertionError):
        generator.generate(
            question="how many orders?",
            semantic_model=assembled.model,
            prose_context=[],
            dialect="duckdb",
        )
    assert recorder.models == ["claude-haiku-4-5"]
