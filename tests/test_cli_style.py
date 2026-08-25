"""The ANSI seam: color only on a TTY, killable three ways, and
piped bytes identical to the uncolored CLI — the contract agents parse by."""

from __future__ import annotations

import io

import pytest

from services.cli import style


@pytest.fixture(autouse=True)
def _reset_override() -> object:
    yield
    style.set_enabled(None)


def test_non_tty_streams_get_plain_bytes() -> None:
    stream = io.StringIO()  # isatty() is False
    assert style.good("PASS", stream) == "PASS"
    assert style.diff_line("+ a: 1") == "+ a: 1"


def test_no_color_env_wins_even_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    assert style.bad("FAIL", Tty()) == "FAIL"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("DST_NO_COLOR", "1")
    assert style.bad("FAIL", Tty()) == "FAIL"


def test_forced_off_beats_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    style.set_enabled(False)  # --no-color
    assert style.accent("basis:", Tty()) == "basis:"


def test_enabled_paints_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("DST_NO_COLOR", raising=False)
    style.set_enabled(True)
    painted = style.good("PASS")
    assert painted.startswith("\033[") and painted.endswith("\033[0m") and "PASS" in painted
    assert style.diff_line("- gone").startswith("\033[31m")
    assert style.diff_line("+ here").startswith("\033[32m")
    assert style.diff_line("@@ hunk @@").startswith("\033[1m")
    assert style.diff_line("plain") == "plain"


def test_meaning_never_rides_color_alone() -> None:
    # The plain text must carry the verdict with color stripped — enforced by
    # construction: helpers wrap the text, never replace it.
    style.set_enabled(True)
    for helper in (style.good, style.bad, style.warn, style.accent, style.dim, style.bold):
        assert "VERDICT" in helper("VERDICT")
