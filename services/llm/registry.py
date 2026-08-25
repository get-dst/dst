"""Provider registry — the one place a model ref becomes a configured LLM client.

BYOK core: providers are pure config (DST_PROVIDERS: name → type + secret +
models) — no vendor is special-cased in code. A model ref is "provider/model",
or a bare model name resolved against the configured providers' model catalogs
(then the default/first declared provider), so pre-BYOK lens bundles keep
resolving without edits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Literal

from services.config import ProviderConfig, resolve_env_ref, settings
from services.contracts.errors import ProviderError
from services.contracts.protocols import Embedder, LLMProvider

log = logging.getLogger("dst")

# The "anthropic" wire type's default tier models. The type IS the vendor's
# protocol, so its model family is type knowledge — config can override both.
_ANTHROPIC_FAST = "claude-haiku-4-5"
_ANTHROPIC_SMART = "claude-sonnet-4-6"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    type: Literal["anthropic", "openai-compatible", "local"]
    api_key: str | None
    key_hint: str  # what to set to supply the secret (for actionable 503s)
    base_url: str | None
    fast_model: str | None
    smart_model: str | None
    models: tuple[str, ...]
    embedding_model: str | None
    embedding_dim: int
    # None = let the model name decide (services/llm/reasoning.py).
    reasoning: bool | None = None


def _spec(name: str, cfg: ProviderConfig) -> ProviderSpec:
    key = cfg.api_key or resolve_env_ref(cfg.api_key_env)
    fast = cfg.fast_model or (_ANTHROPIC_FAST if cfg.type == "anthropic" else None)
    smart = cfg.smart_model or (_ANTHROPIC_SMART if cfg.type == "anthropic" else None)
    hint = cfg.api_key_env or f"DST_PROVIDERS['{name}'].api_key"
    if cfg.type == "local":
        # The pinned in-process model — its band preset keys off this name.
        from services.context.local_embedder import LOCAL_EMBED_DIM, LOCAL_EMBED_MODEL

        default_embed, default_dim = LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM
    else:
        default_embed, default_dim = None, 1024
    return ProviderSpec(
        name,
        cfg.type,
        key,
        hint,
        cfg.base_url,
        fast,
        smart,
        tuple(cfg.models),
        cfg.embedding_model or default_embed,
        cfg.embedding_dim or default_dim,
        cfg.reasoning,
    )


def _configured() -> tuple[dict[str, ProviderConfig], str | None]:
    """Provider config with precedence: env (DST_PROVIDERS) wins; the
    project file (dst.yaml) fills the gap when the env declares nothing."""
    if settings.providers:
        return settings.providers, settings.default_provider
    from services.project.source import project_config

    proj = project_config()
    if proj is not None and proj.providers:
        return proj.providers, settings.default_provider or proj.default_provider
    return {}, settings.default_provider


def specs() -> dict[str, ProviderSpec]:
    """Configured providers, in declared order (order is the tier preference).
    Rebuilt per call (cheap) so tests and reloaded settings see fresh state."""
    providers, _ = _configured()
    return {name: _spec(name, cfg) for name, cfg in providers.items()}


def _owner(model: str, table: dict[str, ProviderSpec]) -> str | None:
    """Which configured provider a bare model name belongs to: the first whose
    catalog or tier models name it; anthropic-type entries claim claude-* names
    (type knowledge); else the default/first declared provider."""
    for name, s in table.items():
        if model in s.models or model in (s.fast_model, s.smart_model):
            return name
    if model.startswith("claude-"):
        for name, s in table.items():
            if s.type == "anthropic":
                return name
        # Type knowledge cuts both ways: no anthropic-type entry means nothing
        # here can serve a claude-* name — unresolved (actionable 503/publish
        # warning), never the default provider (that surfaced as query-time
        # "model not found" against ollama-only installs, since the ModelConfig
        # default is claude-named). A gateway that really serves claude models
        # catalogs them in `models` and is claimed by the pass above.
        return None
    _, default = _configured()
    if default and default in table:
        return default
    return next(iter(table), None)


def default_ref() -> str:
    """The ref an asset that names no model serves on: this install's smart
    tier, else its fast tier. "" when no configured provider carries either.

    BYOK: the lens contract used to default `model:` to a literal claude name,
    so a DeepSeek-only install published green and then 503'd on every question.
    A default model is machinery, not judgment: the org already declared its
    providers, and declaration order already IS its cost policy (see ``tier``),
    so an unnamed model follows that policy instead of a vendor nobody
    configured. Fast is the last resort over unservable — a cheaper answer
    beats no answer, and the publish gate says which tier a lens landed on."""
    return tier("smart") or tier("fast")


def split_ref(ref: str) -> tuple[str | None, str]:
    """'provider/model' → pair; a bare model name is resolved against the
    configured providers (None when nothing is configured); the EMPTY ref means
    "whatever this install serves" (a lens with no `model:` block) and resolves
    through ``default_ref``."""
    if not ref:
        ref = default_ref()
        if not ref:
            return None, ""
    if "/" in ref:
        provider, model = ref.split("/", 1)
        return provider, model
    return _owner(ref, specs()), ref


def wire_name(ref: str) -> str:
    """The model name *ref* actually calls with — the resolved ref's model half.
    A lens that names no model has none of its own, so every trace label, cost
    row and composer must read it from here, never from ``config.model.model``:
    pairing a resolved client with a DIFFERENT name is how a tier fallback used
    to 404."""
    return split_ref(ref)[1]


def tier(name: Literal["fast", "smart"]) -> str:
    """The model ref for a capability tier: the first declared provider with a
    usable key that carries a model for it (declaration order IS the cost
    policy — put the cheap provider first). "" when nothing qualifies."""
    table = specs()
    for s in table.values():
        model = s.fast_model if name == "fast" else s.smart_model
        if s.api_key and model:
            return f"{s.name}/{model}"
    for s in table.values():
        model = s.fast_model if name == "fast" else s.smart_model
        if model:
            return f"{s.name}/{model}"
    return ""


def fast_sibling(ref: str) -> str:
    """The cheap first-tier ref on the SAME provider as *ref* — escalation must
    stay within one provider account. Providers without a fast model get no
    cheap tier (the ref itself comes back)."""
    provider_name, _ = split_ref(ref)
    s = specs().get(provider_name) if provider_name else None
    if s and s.fast_model:
        return f"{s.name}/{s.fast_model}"
    return ref


def key_env_hint(ref: str) -> str:
    provider_name, _ = split_ref(ref)
    spec = specs().get(provider_name) if provider_name else None
    if spec is None:
        return "DST_PROVIDERS — no configured provider covers this model"
    return spec.key_hint


@dataclass(frozen=True)
class ResolvedModel:
    """A configured client together with the model name it must be called with.

    ONE value, deliberately not a client and a name a caller carries separately:
    a REF ("provider/model") and a WIRE NAME ("model") are different things, and
    this project has now paid for confusing them three times — `mgmt_audit` put a
    ref on the wire (404 on every install, 6b2ad65), a lens read its name off
    `config.model` while its client came from the tier (8ab4099), and apply's
    certify self-test sent `deepseek-v4-pro` to Anthropic because the client came
    from the tier fallback and the name from the lens config. Every one of those
    was a client from one resolution paired with a name from another.

    So the name is not passable on its own: pass a ``ResolvedModel``, and the two
    halves can only ever come from the same ``resolve`` call. ``provider`` is
    carried because a SIBLING lookup (``fast_sibling``) must start from the ref
    this client actually came from — the lens's own ref is a different provider
    exactly when a fallback fired, which is the misroute again one tier down."""

    llm: LLMProvider
    provider: str
    name: str

    @property
    def ref(self) -> str:
        """The resolved 'provider/model' ref — what this client came from, which
        is not necessarily the ref the caller asked to resolve."""
        return f"{self.provider}/{self.name}"


def resolve(ref: str) -> ResolvedModel | None:
    """Model ref → client + wire model name, as one ``ResolvedModel``; None when
    unconfigured/keyless.

    Every resolved client is wrapped in RetryingLLM: a 429 or transport
    blip retries with short backoff instead of surfacing as a 502; real API
    errors (auth, bad request) still raise immediately. The provider also carries
    the spec's `reasoning` flag, so call sites keep asking for the ANSWER size
    they want and thinking headroom is added on the wire (services/llm/reasoning.py)."""
    provider_name, model = split_ref(ref)
    spec = specs().get(provider_name) if provider_name else None
    if spec is None or not spec.api_key or not model:
        return None
    from services.llm.retry import RetryingLLM

    if spec.type == "anthropic":
        from services.llm.anthropic_provider import AnthropicProvider

        client: LLMProvider = RetryingLLM(AnthropicProvider(spec.api_key, reasoning=spec.reasoning))
    else:
        from services.llm.openai_compat import OpenAICompatProvider

        assert spec.base_url is not None  # enforced by ProviderConfig validation
        client = RetryingLLM(
            OpenAICompatProvider(
                api_key=spec.api_key, base_url=spec.base_url, reasoning=spec.reasoning
            )
        )
    return ResolvedModel(client, spec.name, model)


def unservable_reason(ref: str) -> str | None:
    """Why *ref* cannot be served by THIS install, or None when it resolves.

    One wording for the publish gate and the 503, so a lens can never publish
    green and then fail to answer: whatever this says is what the query would
    have hit. Each branch names the fix, not just the symptom."""
    if resolve(ref) is not None:
        return None
    table = specs()
    if not table:
        return "no LLM provider is configured — declare one in DST_PROVIDERS or dst.yaml"
    known = ", ".join(sorted(table))
    # Diagnose the RESOLVED ref: an empty ref has already been through
    # ``default_ref`` by the time it reaches a provider, so reporting on the raw
    # string blames the wrong thing (a keyless tier read as "no tier declared").
    provider_name, model = split_ref(ref)
    if provider_name is None:
        if not ref:
            return (
                f"no configured provider declares a fast_model or smart_model, so a lens "
                f"that names none has nothing to run on (configured: {known})"
            )
        return (
            f"no configured provider serves model '{model}' (configured: {known}) — "
            f"list '{model}' under a provider's `models`, or drop the `model:` block "
            "to follow this install's own tier"
        )
    spec = table.get(provider_name)
    if spec is None:
        return f"provider '{provider_name}' is not configured (configured: {known})"
    if not spec.api_key:
        return f"provider '{provider_name}' has no API key — set {spec.key_hint}"
    return f"provider '{provider_name}' cannot serve model '{model}'"


def unservable_detail(ref: str) -> str | None:
    """The whole sentence a gate says when this install cannot serve *ref*, or
    None when it can: WHY, what was TRIED, and what this install actually
    resolves to. One wording, because "fail loudly" has to mean the same three
    facts everywhere — publish, the eval gate, apply's certify self-test and
    `dst test` each used to phrase (or omit) it differently, and the one
    that omitted it silently dialled another vendor instead."""
    reason = unservable_reason(ref)
    if reason is None:
        return None
    # Name what it TRIED, not just what failed: with no `model:` block the ref is
    # the install's own tier, and an operator who is not told which one cannot
    # tell a misconfigured lens from a misconfigured install.
    resolved = ref or default_ref()
    tried = (
        f"'{resolved}'" + ("" if ref else " (this lens names no model)")
        if resolved
        else "this install's own tier, which resolves to nothing"
    )
    return f"{reason}. Tried: {tried} — {serving_summary()}"


def serving_summary() -> str:
    """This install's model resolution, one line: tier → provider/model, plus the
    embedder. The doctor report — rendered into the publish gate's error (the
    moment it is needed) and onto /ready (the operator's standing view)."""
    fast, smart = tier("fast") or "unresolved", tier("smart") or "unresolved"
    embed = embedding_model_name() or "unconfigured"
    return (
        f"configured providers: {', '.join(specs()) or 'none'}; "
        f"fast={fast}; smart={smart}; embeddings={embed}"
    )


# Embedding backends that ship behind an optional extra: type → (module, extra).
# openai-compatible needs none (httpx is core), which is why it's the OSS default.
_EMBED_SDK = {"local": ("fastembed", "local-embed")}


def _embedding_spec() -> ProviderSpec | None:
    """The first declared provider that can ACTUALLY serve embeddings here:
    it names an embedding model, carries a key when its type needs one, and its
    backend is importable in THIS install.

    A declared entry whose extra is missing is skipped (with the fix logged),
    never selected — latching onto an SDK the build doesn't ship 502s every
    embed call, which is worse than no embedder at all (that at least degrades
    legibly via EMBEDDER_HINT). An OSS install with no embedding provider
    declared resolved to a missing SDK and detonated on its first certified apply,
    days after the misconfiguration was made. Importability is checked with
    find_spec — nothing is imported or constructed, so `dst migrate` (via
    ``embedding_identity``) still never triggers a model download."""
    for s in specs().values():
        if not s.embedding_model or s.type not in ("local", "openai-compatible"):
            continue
        if s.type != "local" and not s.api_key:
            continue
        sdk = _EMBED_SDK.get(s.type)
        if sdk is not None and find_spec(sdk[0]) is None:
            log.warning(
                "provider %r (%s) skipped for embeddings: the %s SDK is not installed "
                "— `uv sync --extra %s` to use it",
                s.name,
                s.type,
                sdk[0],
                sdk[1],
            )
            continue
        return s
    return None


EMBEDDER_DOWN = (
    "the embedding model failed to load, so certified matching cannot fire: {detail} "
    "— cached, NOT retried per request; fix the cause (model cache, network, "
    "provider config) and restart the server"
)

# The resolved embedder, cached per configuration identity. Two defects live
# here, and one cache closes both:
#
#   1. Latency. resolve_embedder() constructed a fresh client on EVERY served
#      request — for the local tier a fastembed ONNX session, which costs far
#      more to build than the embedding itself costs to compute, on the
#      certified-serve path, the fastest path the product has.
#   2. Retry storms. A FAILED construction was retried per request, re-attempting
#      the model download every time, so a broken embedder charged every question
#      a download timeout. The failure is cached as its message and re-raised, so
#      a dead embedder costs one attempt per process — see reset_embedder_cache.
#
# Keyed on the configuration that produced it, so changed settings (tests, a
# reloaded config) resolve fresh instead of serving a stale client. No lock:
# construction runs OUTSIDE any critical section deliberately — holding one
# across a model download would wedge every later request behind the daemon
# thread call_bounded abandons on timeout — and dict get/set are atomic under
# the GIL, so the worst a race costs is one duplicate build.
_EMBEDDER_CACHE: dict[tuple[object, ...], Embedder | str] = {}


def reset_embedder_cache() -> None:
    """Forget the cached embedder and any cached failure.

    The explicit recovery path: a failed model load is never retried on its own
    (that is the point — see ``_EMBEDDER_CACHE``), so after fixing the cause,
    either restart the server or call this."""
    _EMBEDDER_CACHE.clear()


def _build_embedder(s: ProviderSpec | None) -> Embedder:
    """Construct the embedder a spec names — or, for None, the implicit local
    tier. Uncached and possibly slow (weights download): go through
    ``resolve_embedder``."""
    if s is None:
        from services.context.local_embedder import LOCAL_EMBED_MODEL, LocalEmbedder

        log.info(
            "no embedding provider declared — using the in-process local embedder (%s)",
            LOCAL_EMBED_MODEL,
        )
        return LocalEmbedder()
    model = s.embedding_model
    assert model is not None  # _embedding_spec skips specs without one
    if s.type == "local":
        from services.context.local_embedder import LocalEmbedder

        return LocalEmbedder(model=model, dim=s.embedding_dim)
    assert s.api_key is not None  # … and keyless non-local ones
    from services.context.openai_embedder import OpenAICompatEmbedder

    assert s.base_url is not None  # enforced by ProviderConfig validation
    return OpenAICompatEmbedder(s.api_key, s.base_url, model, dim=s.embedding_dim)


def resolve_embedder() -> Embedder | None:
    """The install's embedder: the first declared provider that can serve one
    (see ``_embedding_spec``), else the in-process local embedder when its extra
    is installed — the OSS PoC tier, so a zero-config install still embeds
    instead of silently never matching. None when nothing qualifies at all
    (callers degrade or 503 with EMBEDDER_HINT).

    CACHED per configuration (see ``_EMBEDDER_CACHE``): built once, and a
    construction FAILURE is cached too — re-raised as a ProviderError carrying
    ``EMBEDDER_DOWN`` on every later call instead of re-attempted per request."""
    s = _embedding_spec()
    if s is None and find_spec("fastembed") is None:
        return None
    key: tuple[object, ...] = (
        (s.name, s.type, s.embedding_model, s.embedding_dim, s.api_key, s.base_url)
        if s is not None
        else ("<implicit-local>",)
    )
    hit = _EMBEDDER_CACHE.get(key)
    if isinstance(hit, str):
        raise ProviderError("embed", EMBEDDER_DOWN.format(detail=hit))
    if hit is not None:
        return hit
    try:
        embedder = _build_embedder(s)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _EMBEDDER_CACHE[key] = detail
        log.error(
            "embedder failed to load — certified matching is DOWN for this process: %s", detail
        )
        raise ProviderError("embed", EMBEDDER_DOWN.format(detail=detail)) from exc
    _EMBEDDER_CACHE[key] = embedder
    return embedder


def embedding_identity() -> tuple[str, int] | None:
    """The (model, dim) ``resolve_embedder()`` would serve, WITHOUT constructing
    a client (a local embedder's constructor downloads weights — callers like
    `dst migrate` must not trigger that). Mirrors resolution exactly: same
    skip of unusable backends, same implicit local fallback. Band presets and
    the column auto-size both key off this."""
    s = _embedding_spec()
    if s is not None:
        assert s.embedding_model is not None  # _embedding_spec skips specs without one
        return s.embedding_model, s.embedding_dim
    if find_spec("fastembed") is None:
        return None
    from services.context.local_embedder import LOCAL_EMBED_DIM, LOCAL_EMBED_MODEL

    return LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM


def embedding_model_name() -> str | None:
    """The embedding model ``resolve_embedder()`` would serve — see
    ``embedding_identity``."""
    ident = embedding_identity()
    return ident[0] if ident else None


EMBEDDER_HINT = (
    "no embedding provider configured — add one to DST_PROVIDERS: "
    '{"embed": {"type": "local"}} for in-process embeddings (no key; needs the '
    "optional extra `uv sync --extra local-embed` — the PoC tier), an "
    'openai-compatible entry with "embedding_model" + "base_url" set '
    "(production tier)"
)
