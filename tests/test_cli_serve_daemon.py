"""`dst serve --daemon` / `--stop` and the shutdown-signal line.

A server started in the foreground died on a stray signal with nothing in the
log. Background mode records the pid and the log under .dst/, --stop reads
them back, and the lifespan names the signal that ends the server."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path

import pytest

from services import app as app_module
from services.cli import main as cli


class _Child:
    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_daemon_records_pid_and_log_and_reports_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["dst", "serve", "--daemon", "--port", "8123"])
    spawned: list[list[str]] = []

    def popen(cmd: list[str], **kw: object) -> _Child:
        spawned.append(cmd)
        assert kw["start_new_session"] is True
        return _Child()

    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(cli, "_wait_for_health", lambda url, **kw: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    args = argparse.Namespace(host="127.0.0.1", port=8123, reload=False, daemon=True, stop=False)
    assert cli._serve_daemon(args) == 0
    assert spawned[0][1:] == ["-m", "services.cli.main", "serve", "--port", "8123"]
    assert (tmp_path / ".dst" / "serve.pid").read_text().strip() == "4242"
    assert (tmp_path / ".dst" / "serve.log").exists()
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8123" in out and "pid 4242" in out and "serve.log" in out
    # a second --daemon while the first runs is refused, not doubled
    assert cli._serve_daemon(args) == 1
    assert "already running" in capsys.readouterr().out


def test_daemon_child_that_dies_reports_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["dst", "serve", "--daemon"])
    (tmp_path / ".dst").mkdir()
    (tmp_path / ".dst" / "serve.log").write_text("error: schema behind this build\n")
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: _Child(returncode=1))
    args = argparse.Namespace(host="127.0.0.1", port=None, reload=False, daemon=True, stop=False)
    assert cli._serve_daemon(args) == 1
    err = capsys.readouterr().err
    assert "exited with 1" in err and "schema behind this build" in err
    assert not (tmp_path / ".dst" / "serve.pid").exists()


def test_stop_reads_the_pidfile_and_clears_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(stop=True)
    assert cli._serve(args) == 1
    assert "no background server recorded" in capsys.readouterr().err
    pidfile = tmp_path / ".dst" / "serve.pid"
    pidfile.parent.mkdir()
    pidfile.write_text("999999999\n")  # no such process
    assert cli._serve(args) == 1
    assert not pidfile.exists()
    # a live pid gets SIGTERM and the file is removed once it is gone
    sent: list[tuple[int, int]] = []
    alive = {"yes": True}

    def kill(pid: int, sig: int) -> None:
        if sig == 0 and not alive["yes"]:
            raise ProcessLookupError
        if sig == signal.SIGTERM:
            sent.append((pid, sig))
            alive["yes"] = False

    monkeypatch.setattr(os, "kill", kill)
    pidfile.write_text("31337\n")
    assert cli._serve(args) == 0
    assert sent == [(31337, signal.SIGTERM)]
    assert not pidfile.exists()
    assert "stopped background server (pid 31337)" in capsys.readouterr().out


def test_lifespan_names_the_signal_and_hands_it_on(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[int] = []
    previous = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
        app_module.log_shutdown_signals()
        with caplog.at_level(logging.WARNING, logger="dst"):
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pthread_sigmask(signal.SIG_BLOCK, [])  # let the handler run
        assert received == [signal.SIGTERM]
        assert "received SIGTERM; shutting down" in caplog.text
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
