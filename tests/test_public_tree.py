"""Every tracked file in this repository is published as-is on release.

There is no denylist between this tree and the public repo, so the tree itself
has to stay public-ready. Three places have historically collected material that
was never meant to ship: new top-level directories, new docs sections, and
one-off scripts. Each is an allowlist here: adding an entry is a deliberate,
reviewable decision rather than something that rides out unnoticed. Blog drafts
are refused outright, since `draft: true` hides a post from the site, not from
the repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOP_LEVEL = {
    ".dockerignore", ".env.example", ".gitattributes", ".githooks", ".github",
    ".gitignore", ".python-version", "ARCHITECTURE.md", "CHANGELOG.md", "CLAUDE.md",
    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "Dockerfile", "LICENSE", "Makefile",
    "NOTICE", "README.md", "SECURITY.md", "THIRD-PARTY-NOTICES.md", "alembic.ini",
    "apps", "deploy", "docker", "docker-compose.yml", "docs", "fixtures",
    "hatch_build.py", "migrations", "mkdocs.yml", "overrides", "pyproject.toml",
    "scripts", "server.json", "services", "tests", "uv.lock",
}  # fmt: skip

DOCS = {
    "865eaecbc35c0ab54b57dd8393d12684.txt", "assets", "blog", "concepts",
    "deployment.md", "faq.md", "google538d044e7b29d27c.html", "guides", "index.md",
    "llms.txt", "on-screen.md", "quickstart.md", "reference", "robots.txt",
    "security.md", "stylesheets", "upgrading.md",
}  # fmt: skip

SCRIPTS = {
    "__init__.py", "credential_regex.sh", "decision_calibration.py", "gen_env_example.py",
    "gen_jaffle_expansion.py", "genuine_lint.py", "router_experiment.py",
}  # fmt: skip


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert out, "git ls-files returned nothing: the allowlists would pass vacuously"
    return out


def _children(files: list[str], prefix: str) -> set[str]:
    return {f[len(prefix) :].split("/")[0] for f in files if f.startswith(prefix)}


def test_top_level_is_allowlisted() -> None:
    assert _children(_tracked(), "") - TOP_LEVEL == set()


def test_docs_sections_are_allowlisted() -> None:
    assert _children(_tracked(), "docs/") - DOCS == set()


def test_scripts_are_allowlisted() -> None:
    assert _children(_tracked(), "scripts/") - SCRIPTS == set()


def test_no_draft_posts() -> None:
    drafts = [
        f
        for f in _tracked()
        if f.endswith(".md") and re.search(r"^draft:\s*true", (ROOT / f).read_text(), re.MULTILINE)
    ]
    assert drafts == [], "drafts live in the internal repo until published"
