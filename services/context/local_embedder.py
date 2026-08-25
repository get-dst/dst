"""In-process embedding — the PoC tier of the embedder ladder.

fastembed (ONNX, CPU) behind the same `Embedder` seam as every provider: no
API, no key, no GPU. Deliberately pinned to ONE measured model — the serve
bands carry a measured preset keyed by its name, so certified matching
works correctly out of the box instead of silently never firing on a
spread-cosine model. Weights download to the local cache on first use; the
default install stays weight-free (optional extra, like voyage's).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from services.contracts.errors import ProviderError

log = logging.getLogger("dst")

# The pinned model: the only candidate measured whose paraphrase floor
# sits ABOVE its slot-variant ceiling — the property the EXACT band (verbatim
# serve, no gate behind it) requires. Changing this pin requires re-measuring
# and shipping a matching band preset in services/runtime/assembly.py.
LOCAL_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_EMBED_DIM = 384


def cache_dir() -> Path:
    """Where the ONNX weights live — a DURABLE directory, deliberately.

    fastembed defaults its model cache to ``tempfile.gettempdir()/fastembed_cache``
    (fastembed/common/utils.py::define_cache_dir). On macOS that is
    ``/var/folders/…/T``, which the OS reaps — and it did: every blob gone, every
    snapshot symlink dangling, the ONNX session failing NO_SUCHFILE, and with it
    certified matching silently off for a whole session while the answers looked
    ordinary. A model cache is not scratch space. It goes under the user cache
    directory, which nothing sweeps.

    ``FASTEMBED_CACHE_PATH`` still wins — it is fastembed's own knob, honoured
    here because passing ``cache_dir=`` explicitly stops fastembed reading it.
    """
    if env := os.environ.get("FASTEMBED_CACHE_PATH"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "dst" / "fastembed"


class LocalEmbedder:
    def __init__(self, model: str = LOCAL_EMBED_MODEL, dim: int = LOCAL_EMBED_DIM) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ProviderError(
                "local",
                "fastembed not installed — `uv sync --extra local-embed` "
                "(or `pip install 'dst-core[local-embed]'`), or configure an "
                "openai-compatible embedding provider instead",
            ) from exc
        cache = cache_dir()
        log.info("local embedder %s loading from model cache %s", model, cache)
        self._model = TextEmbedding(model, cache_dir=str(cache))
        self.model = model  # embedding_meta identity: (model, dim)
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._model.embed(texts)]
