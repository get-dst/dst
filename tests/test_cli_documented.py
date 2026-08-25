"""The CLI and the docs describe the same tool, in both directions.

Forwards: a verb can ship complete — implementation, apply-time wiring, serving
effects — and still have no line in the reference docs, leaving users unable to
find the tool that solves their problem.

Backwards, and worse: the docs can name a command that was never built. A verb
appearing in a lifecycle line, a guide, an exit-code table and a copy-paste CI
workflow is indistinguishable from a real one until a reader runs it and gets
`invalid choice`. The forward gate cannot see that, because it only ever asks
whether the parser's verbs are written down. So this file asserts both, plus
the same thing one level up: every install line names the package that actually
exists on PyPI.

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
DOCS = next(p for p in (_ROOT / "docs" / "oss" / "docs", _ROOT / "docs") if p.is_dir())
CLI_DOC = DOCS / "reference" / "cli.md"


def _published_pages() -> list[Path]:
    """Everything a stranger reads: the front door plus the whole site."""
    return [_ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]


def _fenced_lines(text: str) -> list[tuple[int, str]]:
    """The lines inside ``` fences — where a reader finds things to run.

    Prose says "plan and apply"; a fence says `dst plan --dir .`, and that is
    the thing that gets pasted into a terminal or a workflow file.
    """
    out: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append((lineno, line))
    return out


# Inside a fence, `dst` at a command position: line start, a shell prompt, a
# pipe or `&&`, a YAML `run:`, a `uv run` prefix, or a lifecycle arrow.
# Anchoring this way rather than matching `dst \w+` anywhere keeps the lines
# that pass `dst` as an ARGUMENT out of the results — `claude mcp add dst
# http://…`, `helm install dst oci://…`.
_INVOCATION = re.compile(r"(?:^|[`(&|;→]|\$ |\brun: |\buv run |\buvx )\s*dst\s+([a-z][a-z0-9_-]*)")
# Anywhere at all, fenced or not: a command a reader can copy out of a sentence.
# `dst validate` in prose is the same false promise as one in a workflow file.
_BACKTICKED = re.compile(r"`dst\s+([a-z][a-z0-9_-]*)")
# `pip install [flags] <package>`. The flag pattern is deliberately strict
# ([A-Za-z-] only, no backticks or punctuation) so that prose citing the command
# — "after every `pip install -U`: `dst serve`" — matches nothing rather than
# reading the next backticked word as the package name.
_INSTALL = re.compile(
    r"\b(pip|pipx) install(?:\s+-[A-Za-z-]+)*\s+[`'\"]?([A-Za-z][A-Za-z0-9._-]*(?:\[[^\]]*\])?)"
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


def test_every_documented_command_exists() -> None:
    """No page may tell a reader to run something the parser will reject."""
    verbs = set(_verbs())
    assert len(verbs) > 20, f"only found {len(verbs)} verbs — the usage parse is probably wrong"

    invented: list[str] = []
    for page in _published_pages():
        text = page.read_text(encoding="utf-8")
        rel = page.relative_to(_ROOT)
        every = list(enumerate(text.splitlines(), 1))
        for pattern, lines in ((_BACKTICKED, every), (_INVOCATION, _fenced_lines(text))):
            for lineno, line in lines:
                for m in pattern.finditer(line):
                    if m.group(1) not in verbs:
                        invented.append(f"{rel}:{lineno}  `dst {m.group(1)}`  in: {line.strip()}")
    invented = sorted(set(invented))
    assert not invented, (
        f"{len(invented)} documented command(s) the CLI does not have:\n"
        + "\n".join(invented)
        + "\n\nDocs describe what exists. Reach for the verb that does the job "
        "(`dst --help` lists them) rather than adding one to match the prose."
    )


def test_every_install_line_names_the_real_package() -> None:
    """The distribution is `dst-core`; `dst` on PyPI is somebody else's project.

    A wrong name in a copy-paste line does not fail — it installs a stranger's
    code onto the reader's machine and then reports `dst: command not found`.
    """
    wrong: list[str] = []
    for page in _published_pages():
        for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            for m in _INSTALL.finditer(line):
                pkg = m.group(2)
                if pkg.split("[")[0] != "dst-core":
                    rel = page.relative_to(_ROOT)
                    wrong.append(f"{rel}:{lineno}  {m.group(1)} install … {pkg}")
    assert not wrong, (
        f"{len(wrong)} install line(s) naming the wrong package (it is `dst-core`):\n"
        + "\n".join(wrong)
    )
