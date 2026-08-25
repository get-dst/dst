"""The embedder ladder — the 'local' provider type, and serve-band
presets keyed by embedder identity (bands are embedder-relative: bge-small
spreads cosines where hosted models compress them).

Selection lives here too: WHICH declared provider may win it (only
one whose backend this install can actually run), and what a zero-config
install falls back to.
"""

from __future__ import annotations

import importlib.util
import logging
import sys

import pytest

from services.config import ProviderConfig, settings
from services.context.local_embedder import LOCAL_EMBED_DIM, LOCAL_EMBED_MODEL
from services.contracts.errors import ProviderError
from services.llm import registry
from services.runtime.assembly import certified_bands

_HAS_FASTEMBED = importlib.util.find_spec("fastembed") is not None


def _use(monkeypatch: pytest.MonkeyPatch, providers: dict[str, ProviderConfig]) -> None:
    monkeypatch.setattr(settings, "providers", providers)


def _installed(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Pretend exactly *modules* are importable. The registry probes optional
    backends with find_spec, so faking it keeps selection tests independent of
    which extras this machine happens to have."""
    monkeypatch.setattr(registry, "find_spec", lambda name: object() if name in modules else None)


class _StubLocal:
    """Stands in for LocalEmbedder: constructing the real one needs the extra
    and downloads weights, and these tests are about SELECTION."""

    def __init__(self, model: str = LOCAL_EMBED_MODEL, dim: int = LOCAL_EMBED_DIM) -> None:
        self.model, self.dim = model, dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


def _stub_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.context.local_embedder.LocalEmbedder", _StubLocal)


def test_local_type_needs_no_key_and_defaults_to_the_pinned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch, "fastembed")
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    spec = registry.specs()["embed"]
    assert spec.embedding_model == LOCAL_EMBED_MODEL
    assert spec.embedding_dim == LOCAL_EMBED_DIM
    # Identity resolves WITHOUT constructing a client (no fastembed needed).
    assert registry.embedding_model_name() == LOCAL_EMBED_MODEL


def test_band_presets_key_off_the_embedder_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pinned local model → its own preset.
    _installed(monkeypatch, "fastembed")
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    bands = certified_bands()
    assert (bands.exact, bands.equiv, bands.assist) == (0.93, 0.80, 0.78)
    # Unknown embedder → the compressed-cosine defaults, unchanged.
    _use(
        monkeypatch,
        {
            "openai": ProviderConfig(
                type="openai-compatible",
                api_key="k",
                base_url="https://api.example.com",
                embedding_model="text-embedding-3-small",
            )
        },
    )
    bands = certified_bands()
    assert (bands.exact, bands.equiv, bands.assist) == (0.95, 0.90, 0.83)
    # No embedder at all → defaults (bands never crash serving).
    _installed(monkeypatch)
    _use(monkeypatch, {})
    assert certified_bands().exact == 0.95


def test_keyless_nonlocal_providers_never_name_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch)
    _use(
        monkeypatch,
        {
            "openai": ProviderConfig(
                type="openai-compatible",
                base_url="https://api.example.com",
                embedding_model="text-embedding-3-small",
            )
        },
    )
    assert registry.embedding_model_name() is None  # no key → resolve would skip it too


@pytest.mark.extra("local-embed")
def test_local_embedder_embeds_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test that wants the REAL backend, so it says so: the marker unmasks
    fastembed for this test alone and skips (naming `uv sync --extra local-embed`)
    when it is absent. Every other test in the suite runs with the extra masked,
    installed or not — see tests/conftest.py."""
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    embedder = registry.resolve_embedder()
    assert embedder is not None
    vecs = embedder.embed(["what was revenue in Q2 2026?", "list open support tickets"])
    assert len(vecs) == 2 and len(vecs[0]) == embedder.dim == 384
    # embedding_meta identity works: (model, dim) — the reindex/guard seam.
    assert getattr(embedder, "model", None) == "BAAI/bge-small-en-v1.5"


# ── a backend this install can't run must never win selection ─────────────────


def test_unusable_voyage_is_skipped_and_the_declared_local_wins(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The detonation: a voyage entry the build cannot serve used to win
    selection and 502 every embed call, days after config time. Skipped now —
    with the fix logged — and resolution falls through to the next candidate."""
    _installed(monkeypatch, "fastembed")
    _stub_local(monkeypatch)
    _use(
        monkeypatch,
        {
            "voyage": ProviderConfig(type="voyage", api_key="k"),
            "embed": ProviderConfig(type="local"),
        },
    )
    with caplog.at_level(logging.WARNING, logger="dst"):
        embedder = registry.resolve_embedder()
    assert isinstance(embedder, _StubLocal)
    assert registry.embedding_identity() == (LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM)
    assert "'voyage'" in caplog.text and "--extra voyage" in caplog.text


def test_declared_local_without_its_extra_is_skipped_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _installed(monkeypatch, "voyageai")
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    with caplog.at_level(logging.WARNING, logger="dst"):
        assert registry.resolve_embedder() is None
    assert registry.embedding_identity() is None
    assert "--extra local-embed" in caplog.text


def test_no_qualifying_provider_falls_back_to_the_implicit_local_tier(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The OSS PoC tier: with the extra present, a zero-config (or all-skipped)
    install embeds in-process instead of silently never matching."""
    _installed(monkeypatch, "fastembed")
    _stub_local(monkeypatch)
    _use(monkeypatch, {"voyage": ProviderConfig(type="voyage", api_key="k")})
    with caplog.at_level(logging.INFO, logger="dst"):
        embedder = registry.resolve_embedder()
    assert isinstance(embedder, _StubLocal) and embedder.dim == LOCAL_EMBED_DIM
    assert registry.embedding_identity() == (LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM)
    assert "no embedding provider declared" in caplog.text


def test_nothing_importable_leaves_the_install_without_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither extra → None, and callers degrade legibly with EMBEDDER_HINT."""
    _installed(monkeypatch)
    _use(monkeypatch, {"voyage": ProviderConfig(type="voyage", api_key="k")})
    assert registry.resolve_embedder() is None
    assert registry.embedding_identity() is None
    _use(monkeypatch, {})
    assert registry.resolve_embedder() is None
    assert registry.embedding_identity() is None


def test_declared_local_beats_the_implicit_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Config drives resolution: the declared entry's own dim wins, and the
    implicit tier stays silent — it is the last resort, not an override."""
    _installed(monkeypatch, "fastembed")
    _stub_local(monkeypatch)
    _use(monkeypatch, {"embed": ProviderConfig(type="local", embedding_dim=512)})
    with caplog.at_level(logging.INFO, logger="dst"):
        embedder = registry.resolve_embedder()
    assert isinstance(embedder, _StubLocal) and embedder.dim == 512
    assert registry.embedding_identity() == (LOCAL_EMBED_MODEL, 512)
    assert "no embedding provider declared" not in caplog.text


def test_identity_probes_with_find_spec_and_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dst migrate` calls identity() — importing fastembed there would
    download weights. Only find_spec may be consulted."""
    probed: list[str] = []

    def fake_find_spec(name: str) -> object:
        probed.append(name)
        return object()

    monkeypatch.setattr(registry, "find_spec", fake_find_spec)
    _use(monkeypatch, {})
    assert registry.embedding_identity() == (LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM)
    assert probed == ["fastembed"]
    # Only the extra('local-embed') test above imports fastembed for real, and only
    # when the extra is installed — so this is the strong assertion everywhere else.
    if not _HAS_FASTEMBED:
        assert "fastembed" not in sys.modules


# ── the embedder is built once per process, and a failed build is not retried ──


def test_the_embedder_is_constructed_once_not_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuilding cost 45.6 ms of a 93 ms certified serve (2.6 ms of it was the
    actual embedding). Construction happens on the first resolve and never again
    for the same configuration."""
    builds: list[int] = []

    class _Counted(_StubLocal):
        def __init__(self, model: str = LOCAL_EMBED_MODEL, dim: int = LOCAL_EMBED_DIM) -> None:
            builds.append(1)
            super().__init__(model, dim)

    _installed(monkeypatch, "fastembed")
    monkeypatch.setattr("services.context.local_embedder.LocalEmbedder", _Counted)
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    first = registry.resolve_embedder()
    assert [registry.resolve_embedder() for _ in range(5)] == [first] * 5
    assert builds == [1]
    # …but a CHANGED configuration resolves fresh: the cache keys on the spec,
    # so a reloaded config never serves the previous install's embedder.
    _use(monkeypatch, {"embed": ProviderConfig(type="local", embedding_dim=512)})
    assert registry.resolve_embedder() is not first
    assert builds == [1, 1]


def test_a_failed_model_load_is_cached_loudly_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The live failure: the OS reaped fastembed's model cache, so every request
    re-attempted the download (a 10 s question took 53 s). One attempt per
    process now — and the raised error names the cause and the recovery."""
    attempts: list[int] = []

    def _explode(model: str = LOCAL_EMBED_MODEL, dim: int = LOCAL_EMBED_DIM) -> _StubLocal:
        attempts.append(1)
        raise RuntimeError("NO_SUCHFILE: model_optimized.onnx")

    _installed(monkeypatch, "fastembed")
    monkeypatch.setattr("services.context.local_embedder.LocalEmbedder", _explode)
    _use(monkeypatch, {"embed": ProviderConfig(type="local")})
    with caplog.at_level(logging.ERROR, logger="dst"):
        with pytest.raises(ProviderError) as first:
            registry.resolve_embedder()
    assert "certified matching is DOWN" in caplog.text
    for _ in range(4):
        with pytest.raises(ProviderError) as again:
            registry.resolve_embedder()
        assert "NO_SUCHFILE" in str(again.value)
        assert "NOT retried per request" in str(again.value)
    assert attempts == [1], "a dead embedder must cost one load attempt per process"
    assert "NO_SUCHFILE" in str(first.value)
    # Recovery is explicit — nothing self-heals, so say so and mean it.
    registry.reset_embedder_cache()
    with pytest.raises(ProviderError):
        registry.resolve_embedder()
    assert attempts == [1, 1]


def test_embedder_skipping_never_touches_generation_tiering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declaration order is still the cost policy for generation — the skip
    logic is embedder selection only."""
    _installed(monkeypatch)
    _use(
        monkeypatch,
        {
            "voyage": ProviderConfig(type="voyage", api_key="k"),
            "cheapco": ProviderConfig(
                type="openai-compatible",
                api_key="sk-d",
                base_url="https://api.cheap.co",
                fast_model="cheap-flash",
            ),
        },
    )
    assert registry.tier("fast") == "cheapco/cheap-flash"
    assert registry.resolve_embedder() is None  # … while nothing serves embeddings
