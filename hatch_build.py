"""Wheel build hook: bundle the built dashboard when one exists.

Copies apps/web/dist -> services/web_dist at wheel-build time, so ANY wheel
built from a source tree with a built SPA ships it — local `uvx --from` and
release CI alike. No SPA built -> API-only wheel; `dst serve` says so
instead of silently serving 404s on /.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface


class CustomBuildHook(BuildHookInterface):  # type: ignore[misc]
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        src = root / "apps" / "web" / "dist"
        dest = root / "services" / "web_dist"
        if not (src / "index.html").exists():
            # Loud, not fatal: an API-only wheel is a legitimate local build,
            # but one shipped silently to a user as "the product" is a long
            # debugging session for whoever installs it.
            self.app.display_warning(
                "no built dashboard at apps/web/dist — building an API-ONLY wheel "
                "(run `pnpm -C apps/web build` first to bundle the UI)"
            )
            return
        if dest.exists():
            for item in dest.iterdir():
                if item.name == ".gitkeep":
                    continue
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(src, dest, dirs_exist_ok=True)
        self.app.display_info(f"dashboard bundled: {src} -> services/web_dist")


class CustomMetadataHook(MetadataHookInterface):  # type: ignore[misc]
    """Make the README's relative links absolute for PyPI.

    GitHub resolves a relative path against the repo; PyPI does not resolve it
    at all, so every figure renders broken and every link dead-ends on the page
    that is the project's front door. Rewriting here rather than in the file
    keeps the README natural to read in the repo, and keeps the two from
    drifting: there is no second copy to forget.

    Images and doc links point at the docs site, which serves the same assets
    and is live before the repository is public. Source paths point at the repo
    on the branch, which is what a reader clicking `services/config.py` wants.
    """

    _SITE = "https://www.dataservetool.com"
    _REPO = "https://github.com/get-dst/dst/blob/main"

    def update(self, metadata: dict[str, Any]) -> None:
        readme = Path(self.root) / "README.md"
        if not readme.is_file():
            return
        text = readme.read_text(encoding="utf-8")
        # The docs tree lifts to docs/ in the public cut and is served at the
        # site root, so one rule covers both spellings.
        text = re.sub(r"(?:docs/oss/)?docs/assets/", f"{self._SITE}/assets/", text)
        text = re.sub(
            r"\]\((?:docs/oss/)?docs/([a-z0-9/_-]+)\.md\)",
            rf"]({self._SITE}/\1/)",
            text,
        )
        # Everything else relative is a file in the repo: code, licences, guides.
        text = re.sub(r"\]\((?!https?://|#)([A-Za-z0-9][\w./-]*)\)", rf"]({self._REPO}/\1)", text)
        metadata["readme"] = {"content-type": "text/markdown", "text": text}
