"""The `dst reindex` command."""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _reindex(args: argparse.Namespace) -> int:
    from services.db.reindex import run_reindex
    from services.llm import registry

    embedder = registry.resolve_embedder()
    if embedder is None:
        print(f"error: {registry.EMBEDDER_HINT}", file=sys.stderr)
        return 1
    return run_reindex(embedder, batch=args.batch)


def register(sub: Any) -> None:
    p = sub.add_parser(
        "reindex",
        help="re-embed all stored vectors with the configured embedder "
        "(after an embedding model/dim change); resumable",
    )
    p.add_argument("--batch", type=int, default=64, help="rows per committed batch")
    p.set_defaults(fn=_reindex)
