"""What `pytest` does in a checkout with no database — and what the gate does.

Two behaviours that pull in opposite directions, which is why both are pinned here.

Someone who has just cloned this repo has no Postgres running, and their first
command is `pytest`. That has to answer in words they can act on, not with a
SQLAlchemy connection-pool traceback, and the tests that need no database have to
run anyway — a green run with a stated skip count is both a fair first impression
and a true one.

The same degradation in a gate would be a lie: a suite that skips everything and
exits 0 reports "passed" while proving nothing, and a green light nobody can turn
red is worse than no light, because people act on it. So `DST_TEST_REQUIRE_DB=1`
(set by `make ci`, `make ci-clean` and the CI workflows) makes the identical
condition a refusal to run.

Both run pytest as a subprocess: the behaviour under test is decided while
tests/conftest.py is imported, which has already happened for this process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# A port nothing listens on, so the probe comes back refused immediately rather
# than after a DNS lookup or a TCP timeout — this is a subprocess in a test.
_NOWHERE = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/nothing"


def _pytest(*args: str, require_db: bool) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, DATABASE_ADMIN_URL=_NOWHERE, DATABASE_URL=_NOWHERE)
    # Both directions set explicitly. Inherited, this switch would arrive already
    # on whenever the suite is itself run by `make ci` — and the degrade case would
    # then be testing the refusal instead, passing for the wrong reason.
    if require_db:
        env["DST_TEST_REQUIRE_DB"] = "1"
    else:
        env.pop("DST_TEST_REQUIRE_DB", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_without_a_database_the_suite_says_so_instead_of_crashing() -> None:
    # One small module is enough: whether the run survives at all is decided in
    # conftest, before any test in it is chosen.
    result = _pytest("tests/test_health.py", require_db=False)
    out = result.stdout + result.stderr

    assert result.returncode == 0, f"a clone without a database must not fail:\n{out}"
    assert "no database" in out, f"the run has to SAY there is no database:\n{out}"
    assert "make up" in out, f"the message has to name the fix:\n{out}"
    # The failure this replaced. Not a substring of any message we write, and the
    # frame that used to be the very first thing a reader saw.
    assert "sqlalchemy/pool" not in out, f"the driver traceback is back:\n{out}"


def test_the_gate_refuses_to_run_without_the_database_it_asked_for() -> None:
    # --collect-only: the refusal fires before collection, so there is nothing to
    # gain by letting it try to run tests it has already decided not to trust.
    result = _pytest("--collect-only", require_db=True)
    out = result.stdout + result.stderr

    assert result.returncode != 0, f"DST_TEST_REQUIRE_DB=1 with no database must fail:\n{out}"
    assert "DST_TEST_REQUIRE_DB" in out, f"the message has to name the switch:\n{out}"
    assert "make up" in out, f"the message has to name the fix:\n{out}"
