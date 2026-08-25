"""ANSI style seam — the ONE module that may emit escape codes.

Direction: an ink ledger, tuned quiet. The structural accent is bold
default-foreground — section labels, the dst voice (`basis:`, `confidence:`) —
a monochrome terminal, no accent hue. red/green/yellow mark verdicts and diffs
only; dim carries meta. Everything else stays the terminal's own ink.

The contract: color appears only when the target stream is a TTY, and
`NO_COLOR`/`DST_NO_COLOR`/`--no-color` kill it. Nothing may encode meaning in
color alone — piped output stays byte-compatible with the uncolored CLI, which
is what agents and the test suite parse. Keeping every escape in this module
makes a future direction change a paint-swap, not a rewrite.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_RESET = "\033[0m"
_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
}

# `--no-color` (or a caller that knows better) flips this; None = decide per
# stream. Tests never see escapes: pytest's captured streams are not TTYs.
_override: bool | None = None


def set_enabled(value: bool | None) -> None:
    """Force color on/off for the process; None returns to auto-detection."""
    global _override
    _override = value


def enabled(stream: TextIO | None = None) -> bool:
    if _override is not None:
        return _override
    if os.environ.get("NO_COLOR") or os.environ.get("DST_NO_COLOR"):
        return False
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def _paint(text: str, *names: str, stream: TextIO | None = None) -> str:
    if not text or not enabled(stream):
        return text
    prefix = "".join(f"\033[{_CODES[n]}m" for n in names)
    return f"{prefix}{text}{_RESET}"


def good(text: str, stream: TextIO | None = None) -> str:
    return _paint(text, "green", "bold", stream=stream)


def bad(text: str, stream: TextIO | None = None) -> str:
    return _paint(text, "red", "bold", stream=stream)


def warn(text: str, stream: TextIO | None = None) -> str:
    return _paint(text, "yellow", stream=stream)


def accent(text: str, stream: TextIO | None = None) -> str:
    """The structural accent: bold, never a hue. Kept distinct from `bold` so
    callers state intent and a future direction change stays a paint-swap."""
    return _paint(text, "bold", stream=stream)


def dim(text: str, stream: TextIO | None = None) -> str:
    return _paint(text, "dim", stream=stream)


def bold(text: str, stream: TextIO | None = None) -> str:
    return _paint(text, "bold", stream=stream)


def diff_line(line: str) -> str:
    """One plan-diff line, painted by its prefix — what makes a long plan
    skimmable."""
    if line.startswith("+++") or line.startswith("---"):
        return dim(line)
    if line.startswith("@@"):
        return accent(line)
    if line.startswith("+"):
        return _paint(line, "green")
    if line.startswith("-"):
        return _paint(line, "red")
    return line
