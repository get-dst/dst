"""The `dst calibrate-bands` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from services.cli._common import (
    _adopt_project_env,
)


def _calibrate_bands(args: argparse.Namespace) -> int:
    """Fit the certified serve bands for the configured embedder from the
    project's certified questions and a labelled pairs file the user writes,
    and store them for that model. Refuses, saying how many more pairs are
    needed, when there are too few to fit; `--dry-run` reports without
    storing."""
    import yaml

    from services.certify import bands
    from services.llm import registry

    _adopt_project_env(args)
    root = Path(getattr(args, "dir", ".") or ".")
    embedder = registry.resolve_embedder()
    model = registry.embedding_model_name()
    if embedder is None or model is None:
        print(f"error: {registry.embedder_gap() or registry.EMBEDDER_HINT}", file=sys.stderr)
        return 1
    corpus: dict[str, str] = {}
    for path in sorted(root.glob("lenses/*/certified_answers.yaml")):
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("slots"):
                continue  # a template never serves on similarity alone
            if str(entry.get("status", "active")) == "retired":
                continue
            question, sql = str(entry.get("question") or "").strip(), entry.get("sql")
            if question and isinstance(sql, str):
                corpus[question] = sql
    if not corpus:
        print(
            f"error: no certified answers under {root}/lenses/*/certified_answers.yaml",
            file=sys.stderr,
        )
        return 1
    try:
        pairs = bands.parse_pairs(yaml.safe_load(Path(args.pairs).read_text(encoding="utf-8")))
        result = bands.fit(model, corpus, pairs, embedder.embed)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {args.pairs}: {exc}", file=sys.stderr)
        return 1
    for line in result.lines():
        print(line)
    if result.bands is None:
        print(f"error: {result.refused}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("dry run: nothing stored")
        return 0
    bands.store(model, result)
    print(f"stored for {model}; a running server reads it within a minute")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "calibrate-bands",
        help="fit the certified serve bands for the configured embedder from labelled "
        "paraphrase pairs, and store them for that model",
    )
    p.add_argument(
        "--pairs",
        required=True,
        help="YAML file: `pairs:` a list of {asked, certified, serves: true|false}",
    )
    p.add_argument("--dir", default=".", help="the project (default: .)")
    p.add_argument("--dry-run", action="store_true", help="report the fit, store nothing")
    p.set_defaults(fn=_calibrate_bands)
