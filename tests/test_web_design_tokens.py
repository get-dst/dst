"""The dashboard must not regrow generic-LLM styling — or the amber accent.

The identity is warm paper + ink + mono; stock indigo/purple ramps, gradient
text, and rounded-2xl card soup are the generic-LLM fingerprints this pins
out. Brand-amber hexes and
Tailwind's stock amber scale are banned too — warning states use the semantic
tokens (text-amber / bg-amber-bg / border-amber-strong), whose #b45309 value
lives only in index.css.
"""

import re
from pathlib import Path

WEB_SRC = Path(__file__).resolve().parent.parent / "apps" / "web" / "src"

BANNED = re.compile(
    r"indigo-\d|violet-\d|purple-\d|rounded-2xl|bg-clip-text|bg-gradient-to"
    r"|#4338ca|#7c3aed|#8b5cf6"
    r"|#d97706|#f59e0b|#fbbf24|#fef3c7|#fef9ee|#92400e|amber-\d",
    re.IGNORECASE,
)


def test_no_vibe_styling_in_web() -> None:
    hits = []
    for path in sorted(WEB_SRC.rglob("*")):
        if path.suffix not in {".tsx", ".ts", ".css", ".html"}:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if BANNED.search(line):
                hits.append(f"{path.relative_to(WEB_SRC)}:{lineno}: {line.strip()[:80]}")
    assert not hits, (
        "generic-LLM styling tells found (see docs/design/genuine-signature.md):\n"
        + "\n".join(hits)
    )
