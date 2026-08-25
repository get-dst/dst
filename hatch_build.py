"""Wheel build hook: bundle the built dashboard when one exists.

Copies apps/web/dist -> services/web_dist at wheel-build time, so ANY wheel
built from a source tree with a built SPA ships it — local `uvx --from` and
release CI alike. No SPA built -> API-only wheel; `dst serve` says so
instead of silently serving 404s on /.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


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
