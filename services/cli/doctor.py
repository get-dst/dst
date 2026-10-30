"""The `dst doctor` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from services.cli import style
from services.cli._common import (
    _adopt_project_env,
)


def _certified_corpus_files(root: Path) -> list[str]:
    """The project's certified_answers.yaml files that hold at least one
    entry (a comment-only scaffold file is not a corpus)."""
    import yaml

    found: list[str] = []
    for path in sorted(root.glob("lenses/*/certified_answers.yaml")):
        try:
            entries = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — plan reports malformed YAML; doctor only counts
            continue
        if isinstance(entries, list) and entries:
            found.append(path.relative_to(root).as_posix())
    return found


def _doctor(args: argparse.Namespace) -> int:
    """Callability, not configuration: /ready reports what is
    CONFIGURED and is deliberately never a readiness gate, so a fully-configured
    but uncallable provider (SDK incompatibility, bad key, wrong base_url) was
    only discoverable via a real query's 500 and a server-log traceback. One
    cheap REAL call per model tier — max_tokens=1 — catches the whole class for
    a fraction of a cent. In-process like `dst test`; connections are already
    probed by every apply and are not re-probed here."""
    _adopt_project_env(args)
    from services.contracts.protocols import Message
    from services.db.schema_state import schema_state
    from services.llm import registry
    from services.observability import usage as ai_usage

    failures = 0
    try:
        state = schema_state()
        print(f"db            {state.summary()}")
        if state.status == "behind":
            failures += 1
    except Exception as exc:  # noqa: BLE001 — every probe reports, none aborts the report
        print(f"db            FAIL — {exc}")
        failures += 1
    root = Path(getattr(args, "dir", ".") or ".")
    try:
        embedder = registry.resolve_embedder()
        if embedder is not None:
            print("embeddings    ok")
        elif corpus := _certified_corpus_files(root):
            # A corpus with no embedder is a corpus that never serves: every
            # certified answer stores unembedded and matching is silently off.
            # That is a failure of this install, in red, not a note.
            print(
                style.bad(
                    "embeddings    FAIL — not configured, and this project certifies "
                    f"answers ({', '.join(corpus)}): certified matching is OFF, nothing "
                    "certified will serve. Add an embedding provider "
                    "(`pip install 'dst-core[local-embed]'` for the in-process one)"
                )
            )
            failures += 1
        else:
            print("embeddings    not configured — certified matching off")
    except Exception as exc:  # noqa: BLE001
        print(f"embeddings    FAIL — {exc}")
        failures += 1
    if note := registry.unpriced_note():
        # Not a failure of callability, but a cost line that is a floor.
        print(style.warn(f"pricing       {note}"))
    else:
        print("pricing       ok")
    print("providers")
    for tier_name in ("fast", "smart"):
        ref = registry.tier(tier_name)
        pair = registry.resolve(ref)
        if pair is None:
            detail = registry.unservable_detail(ref) or "no provider configured"
            print(f"  {tier_name:<6}{ref or '(unset)'}  SKIP — {detail}")
            continue
        try:
            with ai_usage.purpose("other"):
                pair.llm.complete(
                    system=[],
                    messages=[Message(role="user", content="ping")],
                    model=pair.name,
                    temperature=0.0,
                    # 8, not 1: a 1-token cap is consumed before any content, so
                    # completion_text logs an 'empty completion' warning on a call
                    # that SUCCEEDED — a false alarm in the one verb whose job is to
                    # say what works. Still a fraction of a cent; this probes
                    # callability, not output.
                    max_tokens=8,
                )
            print(f"  {tier_name:<6}{pair.name}  ok")
        except Exception as exc:  # noqa: BLE001 — the whole point is naming this
            failures += 1
            print(f"  {tier_name:<6}{pair.name}  FAIL — {exc}")
    # The scaffold's own currency. Never a failure — an author may be holding an
    # older skill deliberately, and this verb's exit code means "callable", not
    # "up to date". It exists because an init-time snapshot outliving its
    # release had no reader at all (#51).
    from services.cli.init import stale_agent_files

    failures += __import__("services.cli.cube", fromlist=["doctor_row"]).doctor_row(root)
    # Which lane serves each published lens, and what a Cube lane lacks. A
    # missing dependency is a warning, never a failure: driver-mode agents
    # are served without a chat tier, and an unmonitored lens still serves.
    from services.serve import lanes

    for line in lanes.lines(lanes.report()):
        print(style.warn(line) if line.lstrip().startswith("!") else line)
    if (root / "dst.yaml").exists():
        stale = stale_agent_files(root)
        print(
            f"skills        {len(stale)} behind this dst — `dst init . --skills-only`"
            if stale
            else "skills        current"
        )
    if failures:
        print(style.bad(f"doctor: {failures} check(s) failed"), file=sys.stderr)
    return 1 if failures else 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "doctor",
        help="can this install actually run? DB schema, embedder, and one cheap "
        "REAL call per model tier — catches a configured-but-uncallable provider "
        "before a query 500s",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env supplies DATABASE_URL and provider keys (default: .)",
    )
    p.set_defaults(fn=_doctor)
