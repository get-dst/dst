"""Every CLI verb appears in the published reference.

A verb can ship complete — implementation, apply-time wiring, serving effects —
and still have no line in the reference docs, leaving users unable to find the
tool that solves their problem. The class recurs because docs live in a
different file from the verb and nothing connects them, so this is a gate
rather than a review item.

The parser is the source of truth, not a hand-kept list — a list drifts the
same way the docs do.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

# The docs tree lives at docs/oss/docs/ or docs/ depending on layout — this
# gate must hold in both, so resolve whichever exists.
_ROOT = Path(__file__).resolve().parents[1]
CLI_DOC = next(
    p
    for p in (
        _ROOT / "docs" / "oss" / "docs" / "reference" / "cli.md",
        _ROOT / "docs" / "reference" / "cli.md",
    )
    if p.exists()
)


def _verbs() -> list[str]:
    """The subcommands argparse itself advertises, read out of `--help`.

    In-process (no subprocess): `main()` builds its parser inline, so the usage
    line is the only place the full set is stated once.
    """
    import sys

    from services.cli.main import main

    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["dst", "--help"]
    try:
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            main()
    finally:
        sys.argv = argv
    m = re.search(r"\{([a-z,\-]+)\}", buf.getvalue())
    assert m, "could not read the subcommand list out of `dst --help`"
    return sorted(m.group(1).split(","))


def test_every_cli_verb_is_in_the_published_reference() -> None:
    doc = CLI_DOC.read_text(encoding="utf-8")
    verbs = _verbs()
    assert len(verbs) > 20, f"only found {len(verbs)} verbs — the usage parse is probably wrong"

    undocumented = [v for v in verbs if f"`dst {v}" not in doc]
    assert not undocumented, (
        f"{len(undocumented)} CLI verb(s) missing from {CLI_DOC}: "
        f"{undocumented}. A verb users cannot find is a verb that does not exist for them — "
        f"add a `### \\`dst <verb>\\`` section saying what it is FOR, not just its flags."
    )
