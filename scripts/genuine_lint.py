#!/usr/bin/env python3
"""The genuineness gate, mechanical layer.

Lints the shipped UI surfaces for the regex-visible tells of a generic
generated frontend: gradient/glass effects, emoji and sparkle decoration,
marketing-fluff vocabulary, hedge verbs, and em-dash pile-ups. Layout and
tone problems a regex can't see are a human review matter; this script must
not pretend to cover them.

A line containing `genuine-allow` is waived (for the rare legitimate hit,
e.g. the skeleton shimmer's linear-gradient). Real exit code: 1 on any hit,
0 clean — wired into `make ci` beside ruff/mypy, never behind a pipe.

Self-test: `genuine_lint.py --self-test` plants one offender per rule in a
temp tree and asserts every rule fires, so a rule that silently stopped
matching is caught (a pytest wrapper pins this).
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The rendered surfaces only. Documentation prose is deliberately out of
# scope: long-form writing legitimately uses constructions these rules reject
# in UI copy, and scoping the gate beats calibrating its thresholds.
SCOPE = ["apps/web/src", "landing.html", "site.css"]
EXTS = {".tsx", ".ts", ".css", ".html"}
SKIP_PARTS = {"node_modules", "dist", ".test."}

# Rule -> (tell id, compiled regex, message). Emoji classes cover the
# decoration ranges; the mono status glyphs (checkmark/cross/middle dot,
# U+2713/U+2717/U+00B7) are NOT in these ranges by construction — the lint
# must never fight the dashboard's own mono glyph set.
RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "V2",
        re.compile(r"\bnot just\b|\bmore than just\b|\bisn'?t just\b", re.IGNORECASE),
        "elevation construction ('not just X…') — state Y instead",
    ),
    (
        "V5",
        re.compile(
            r"\bcan help you\b|\bdesigned to\b|\ballows you to\b|\baims to\b|\bhelps you to\b",
            re.IGNORECASE,
        ),
        "hedge verb — state the claim flat with evidence, or cut it",
    ),
    (
        "U1",
        re.compile(r"[\U0001F300-\U0001FAFF⭐❗❤]"),
        "emoji decoration in a shipped surface",
    ),
    (
        "U2",
        re.compile(r"backdrop-filter|linear-gradient|radial-gradient|conic-gradient"),
        "gradient/glass — the identity is paper and hairlines (genuine-allow waives real cases)",
    ),
    (
        "U3",
        re.compile(r"[✨⭐\U0001F31F\U0001F4AB]|sparkle"),
        "sparkle mark — this product's stance is governed AI, not magic AI",
    ),
    (
        "U5",
        re.compile(
            r"\bseamless\w*\b|\bleverag\w+\b|\bunlock\w*\b|\bsupercharg\w+\b"
            r"|\beffortless\w*\b|\bgame.chang\w+\b|\bempower\w*\b",
            re.IGNORECASE,
        ),
        "marketing-fluff vocabulary",
    ),
]

# V1 is a density rule, handled separately: more than two em-dashes on one
# line of user-facing text reads as machine-generated cadence.
V1_MAX_PER_LINE = 2


def files_in_scope(root: Path) -> list[Path]:
    out: list[Path] = []
    for entry in SCOPE:
        p = root / entry
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(
                f
                for f in sorted(p.rglob("*"))
                if f.suffix in EXTS and not any(part in str(f) for part in SKIP_PARTS)
            )
    return out


def lint(root: Path) -> list[str]:
    hits: list[str] = []
    for f in files_in_scope(root):
        rel = f.relative_to(root)
        prev = ""
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # A waiver covers its own line and the next one, so a comment can
            # sit above a multi-line declaration.
            waived = "genuine-allow" in line or "genuine-allow" in prev
            prev = line
            if waived:
                continue
            for tell, rx, msg in RULES:
                if rx.search(line):
                    hits.append(f"{rel}:{lineno}: [{tell}] {msg}")
            if line.count("—") > V1_MAX_PER_LINE:
                hits.append(
                    f"{rel}:{lineno}: [V1] {line.count(chr(0x2014))} em-dashes in one line — "
                    "periods and colons do most joins"
                )
    return hits


def self_test() -> int:
    """Plant one offender per rule; every rule must fire, and a waived line
    must not. A silent rule is a vacuous pass — the worst state."""
    planted = {
        "V2": "<p>It's not just a dashboard.</p>",
        "V5": "<p>dst is designed to help.</p>",
        "U1": "<span>\U0001f680</span>",
        "U2": "background: linear-gradient(#fff, #000);",
        "U3": "<span>✨ AI magic</span>",
        "U5": "<p>Unlock seamless insights.</p>",
        "V1": "<p>one — two — three — chained</p>",
        "waived": "background: linear-gradient(#fff, #000); /* genuine-allow */",
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "apps/web/src").mkdir(parents=True)
        for name, content in planted.items():
            (root / "apps/web/src" / f"{name}.html").write_text(content, encoding="utf-8")
        hits = lint(root)
        fired = {h.split("[")[1].split("]")[0] for h in hits}
        expected = {"V1", "V2", "V5", "U1", "U2", "U3", "U5"}
        missing = expected - fired
        waived_hit = any("waived" in h for h in hits)
        if missing:
            print(f"SELF-TEST FAILED: rules never fired: {sorted(missing)}", file=sys.stderr)
            return 1
        if waived_hit:
            print("SELF-TEST FAILED: genuine-allow waiver did not waive", file=sys.stderr)
            return 1
        print(f"self-test: all {len(expected)} rules fire, waiver waives")
        return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    hits = lint(ROOT)
    if hits:
        print("\n".join(hits))
        print(
            f"\nGENUINE GATE FAILED: {len(hits)} tell(s) in shipped surfaces "
            "(waive a legitimate hit with a genuine-allow comment on the line)",
            file=sys.stderr,
        )
        return 1
    print(f"genuine: clean ({len(files_in_scope(ROOT))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
