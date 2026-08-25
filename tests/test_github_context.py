"""GitHub context fetch (the seam behind POST /mgmt/lenses/{name}/context/github).

Offline: monkeypatched contents-API responses, no network. Live test is gated by
DST_TEST_GITHUB=1 (+ optionally GITHUB_TOKEN / GITHUB_TEST_REPO).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

import services.connectors.github as gh_mod
from services.connectors.github import fetch_paths

run_live = pytest.mark.skipif(
    not os.environ.get("DST_TEST_GITHUB"),
    reason="set DST_TEST_GITHUB=1 (+ optionally GITHUB_TOKEN / GITHUB_TEST_REPO) to run",
)

# owner/repo contents keyed by path: a dir walk with mixed file types.
_TREE: dict[str, Any] = {
    "models": [
        {"type": "file", "name": "orders.sql", "path": "models/orders.sql", "content": ""},
        {"type": "dir", "name": "staging", "path": "models/staging"},
        {"type": "file", "name": "app.py", "path": "models/app.py", "content": ""},
    ],
    "models/staging": [
        {"type": "file", "name": "schema.yml", "path": "models/staging/schema.yml", "content": ""},
    ],
    "README.md": {"type": "file", "name": "README.md", "path": "README.md", "content": ""},
}


def _fake_get_json(url: str, token: str | None) -> Any:
    path = url.split("/contents/")[1].split("?")[0]
    return _TREE[path]


def test_fetch_paths_walks_dirs_and_filters_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh_mod, "_get_json", _fake_get_json)
    monkeypatch.setattr(gh_mod, "_file_content", lambda item: f"content of {item['path']}")

    files = fetch_paths("owner/repo", ["models", "README.md"])

    got = dict(files)
    # .sql/.yml/.md survive — including via the recursive dir walk; .py is excluded.
    assert set(got) == {"models/orders.sql", "models/staging/schema.yml", "README.md"}
    assert got["README.md"] == "content of README.md"


def test_fetch_paths_decodes_inline_base64_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    doc = {
        "type": "file",
        "name": "notes.md",
        "path": "notes.md",
        "content": base64.b64encode(b"churn = 90 days inactive").decode(),
    }
    monkeypatch.setattr(gh_mod, "_get_json", lambda url, token: doc)

    files = fetch_paths("owner/repo", ["notes.md"])
    assert files == [("notes.md", "churn = 90 days inactive")]


@run_live
def test_live_github() -> None:
    token = os.environ.get("GITHUB_TOKEN") or None
    repo = os.environ.get("GITHUB_TEST_REPO", "octocat/Spoon-Knife")
    files = fetch_paths(repo, ["README.md"], token=token)
    assert files and all(content for _path, content in files)
