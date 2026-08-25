"""Load the server's project file (dst.yaml), if one exists.

Precedence everywhere: explicit env settings win; the project file fills the
gaps. The load is cached per path+mtime so the dev loop picks up edits without
a restart while the serving path stays cheap. A malformed file is logged and
ignored — a broken dst.yaml must never take the API down.
"""

from __future__ import annotations

import logging
from pathlib import Path

from services.config import settings
from services.project.schema import ProjectConfig, parse_project_yaml

log = logging.getLogger("dst")

_cache: tuple[str, float, ProjectConfig] | None = None


def project_config() -> ProjectConfig | None:
    global _cache
    path = Path(settings.project_file)
    try:
        stat = path.stat()
    except OSError:
        return None
    if _cache and _cache[0] == str(path) and _cache[1] == stat.st_mtime:
        return _cache[2]
    try:
        cfg = parse_project_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("dst.yaml at %s failed to parse — ignoring it", path)
        return None
    _cache = (str(path), stat.st_mtime, cfg)
    return cfg
