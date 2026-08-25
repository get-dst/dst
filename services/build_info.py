"""Build identity for the running process: any number of fixes can land while
/health reports the same pyproject version throughout — a stale process is
indistinguishable from current code, so a fix that never got deployed reads as
a fix that did not work. Best-effort git capture, once at import
(startup); any failure — packaged install without .git, no git binary, timeout
— degrades to None. This must never add a startup failure mode."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _capture() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[1]
    # Wheels/images ship no .git next to the package — and a venv that happens
    # to live inside some OTHER repo must not report that repo's SHA.
    if not (root / ".git").exists():
        return None, None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
        if not sha:
            return None, None
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout
        return sha, bool(porcelain.strip())
    except Exception:  # noqa: BLE001 — identity is informational, never fatal
        return None, None


GIT_SHA, GIT_DIRTY = _capture()
