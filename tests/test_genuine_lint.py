"""The genuineness gate stays armed.

Two invariants: every rule fires on a planted offender (a silent rule is a
vacuous pass — the gate's own worst state), and committed HEAD is clean (the
same check `make ci` runs, pinned here so the suite catches a regression even
when someone edits surfaces without running make).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT = ROOT / "scripts" / "genuine_lint.py"


def test_every_rule_fires_on_a_planted_offender() -> None:
    r = subprocess.run(
        [sys.executable, str(LINT), "--self-test"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_head_surfaces_are_clean() -> None:
    r = subprocess.run([sys.executable, str(LINT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
