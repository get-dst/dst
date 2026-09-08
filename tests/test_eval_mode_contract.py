"""Every mode a writer persists must parse (mode drift).

``EvalRun.mode`` declared ``regression | health | behavioral`` while the CLI
wrote ``test`` on every ``dst test`` sweep. The column is plain text, so nothing
rejected it and the dashboard happily rendered TEST badges — but a strict parse
of eval_run rows failed on exactly the runs a human triggers by hand.

This test is the tripwire: it reads the mode literals straight out of the
contract and asserts the set matches what the writers use. Add a writer with a
new mode and this fails until the contract admits it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from services.contracts.eval import EvalRun

ROOT = Path(__file__).resolve().parents[1]

# (file, the literal it passes as an eval-run mode) — the writers, by hand,
# because grepping for them is what let one drift in the first place.
WRITERS = {
    "services/evals/service.py": "regression",
    "services/cli/main.py": "test",
    "services/api/mgmt_evals.py": "health",
    "services/evals/runner.py": "behavioral",
}


def _declared() -> set[str]:
    return set(get_args(EvalRun.model_fields["mode"].annotation))


@pytest.mark.parametrize(("path", "mode"), sorted(WRITERS.items()))
def test_every_writers_mode_is_declared(path: str, mode: str) -> None:
    assert mode in _declared(), (
        f"{path} persists mode={mode!r}, which EvalRun.mode does not allow: {sorted(_declared())}"
    )


@pytest.mark.parametrize(("path", "mode"), sorted(WRITERS.items()))
def test_the_writer_still_writes_that_mode(path: str, mode: str) -> None:
    """Keeps the table above honest: if a writer changes, notice here."""
    src = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(rf'["\']{mode}["\']', src), (
        f"{path} no longer mentions mode {mode!r} — update WRITERS (and check "
        "whether the contract literal should shrink)"
    )


def test_an_unknown_mode_is_still_rejected() -> None:
    """Widening the literal must not turn it into a free-text field."""
    with pytest.raises(ValueError):
        EvalRun.model_validate(
            {"id": "r1", "lens": "l", "started_at": "2026-09-05T00:00:00Z", "mode": "whatever"}
        )
