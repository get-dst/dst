"""GitHub context connector — fetch selected files from a repo (public or token).

Walks the given paths via the GitHub contents API, returning (path, content) for
SQL / YAML / Markdown files (dbt models, schema docs, READMEs). Stdlib only.
Feeds POST /mgmt/lenses/{name}/context/github (services/context/ingest.py).
"""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"
_EXTS = (".sql", ".yml", ".yaml", ".md")


def _get_json(url: str, token: str | None) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dst"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "dst"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return str(resp.read().decode("utf-8", "replace"))


def fetch_paths(
    repo: str, paths: list[str], ref: str = "main", token: str | None = None
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in paths:
        _walk(repo, path, ref, token, out)
    return out


def _walk(repo: str, path: str, ref: str, token: str | None, out: list[tuple[str, str]]) -> None:
    data = _get_json(f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={ref}", token)
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "dir":
            _walk(repo, str(item["path"]), ref, token, out)
        elif item.get("type") == "file" and str(item.get("name", "")).endswith(_EXTS):
            out.append((str(item["path"]), _file_content(item)))


def _file_content(item: dict[str, Any]) -> str:
    download = item.get("download_url")
    if download:
        return _get_text(str(download))
    return base64.b64decode(str(item.get("content", ""))).decode("utf-8", "replace")
