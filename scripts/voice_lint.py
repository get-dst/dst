#!/usr/bin/env python3
"""Public-voice lint: shipped files teach the reader about the product.

Everything in this repository that ships is read by strangers — code comments,
docstrings, CLI messages, the scaffold written into a user's own project, the
dashboard, the tests, the docs. This gate rejects the writing habits that serve
an internal audience instead:

* issue or tracker identifiers ("staging #12", "(issue #5, 4e)") — unresolvable
  by anyone outside the private repo;
* names of internal experiments and runs;
* our own measurements presented as product facts ("in our runs", "we measured",
  "a 720-run study"), which a reader cannot verify;
* roadmap and commercial narration ("pending extraction", "moves to the paid
  tier") — a promise, not documentation;
* slogans written for the team rather than the reader.

Keep the engineering reason, drop the internal framing. A comment explaining why
a guard exists is an asset; the ticket number that prompted it is noise.

Real exit code: 1 on any hit, 0 clean. Wired into `make ci` so a violation is
caught before it is pushed, not after.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories whose contents ship. Anything outside these is private working
# material and is not this gate's business.
SHIPPED = (
    "services",
    "tests",
    "apps/web/src",
    "migrations",
    "scripts",
    "docs/oss/docs",  # the published site — the surface strangers read most
)
# Named one by one because the suffix filter below cannot see them: a shipped
# file with no extension (or one that IS its extension) is exactly where an
# internal aside hides longest, since no sweep of the tree ever looks at it.
SHIPPED_FILES = (
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".gitignore",
    ".dockerignore",
    "Dockerfile",
    "Makefile",
    "NOTICE",
)
# Applies to the directory sweep only; SHIPPED_FILES are read regardless.
SUFFIXES = {".py", ".ts", ".tsx", ".md", ".yaml", ".yml", ".sh", ".css"}

# Never lint the gate's own vocabulary, or research scripts that never ship.
# The vocabulary gates carry banned phrases as DATA, not prose, and neither
# ships (both are denylisted from the public cut).
EXEMPT = (
    "scripts/voice_lint.py",
    "scripts/extract_public.sh",
    "scripts/public_exclude.txt",
    "tests/test_docs_vocabulary.py",
)
EXEMPT_GLOBS = ("scripts/exp_*.py", "scripts/enforcement_experiment.py")

RULES: list[tuple[str, str]] = [
    (
        r"(?i)(staging )?issue #\d+|staging #\d+",
        "tracker id — say what the code does, not which ticket asked",
    ),
    (r"(?i)chronicle lap|probe harvest|fleet episode|Act-\d", "internal experiment name"),
    (
        r"(?i)in our (runs|tests|probes)|we measured|our internal (runs|testing)",
        "our measurement — a reader cannot check it",
    ),
    (
        r"(?i)measured (on|against) a (real|live|big|mock)|\d+-run study",
        "our measurement — keep the rule, drop the study",
    ),
    (
        r"(?i)pending extraction|at extraction|moves to the paid",
        "roadmap narration — document what exists",
    ),
    (
        r"(?i)gate that cannot fail|ticket that dies|empty suite is not a pass",
        "team slogan — state the behaviour instead",
    ),
    (
        r"SECRET-AUDIT|HANDS-ON-FINDINGS|OSS-RELEASE-AUDIT|RELEASE-GATES|REPLAY-CURRENT",
        "internal document a reader cannot open",
    ),
    (
        r"(?i)\b(this|the) epic\b|\bwork item\b",
        "our planning vocabulary — describe the behaviour instead",
    ),
]


def _files() -> list[Path]:
    out: list[Path] = []
    for d in SHIPPED:
        base = ROOT / d
        if base.is_dir():
            out += [p for p in base.rglob("*") if p.suffix in SUFFIXES and p.is_file()]
    out += [ROOT / f for f in SHIPPED_FILES if (ROOT / f).is_file()]
    return out


def _exempt(rel: str) -> bool:
    if rel in EXEMPT:
        return True
    return any(Path(rel).match(g) for g in EXEMPT_GLOBS)


def main() -> int:
    hits: list[str] = []
    for path in _files():
        rel = str(path.relative_to(ROOT))
        if _exempt(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, why in RULES:
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"  {rel}:{line}  {m.group(0)!r} — {why}")
    if hits:
        print("\nVOICE GATE FAILED: shipped files carry internal-audience writing\n")
        print("\n".join(sorted(hits)))
        print(f"\n{len(hits)} hit(s). Keep the engineering reason, drop the internal reference.\n")
        return 1
    print(f"voice: clean ({len(_files())} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
