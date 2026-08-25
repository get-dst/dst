"""No server up must cost one line, not a 69-line traceback.

On every server-bound verb — `dst plan`, `keys list`, `query`, `export`,
`introspect` … — nothing listening means
`httpx.ConnectError: [Errno 61] Connection refused` under dozens of lines of
stack. Catching `httpx.TimeoutException` is not enough: a slow server and an
absent one raise different exceptions, so the connect half needs its own
coverage on every verb.

The catch is at the DISPATCH (`main()`), not per verb: 16 verbs make httpx calls
and a rule each of them has to remember is a rule the 17th will not. httpx is
monkeypatched — no server, no DB.
"""

from __future__ import annotations

import argparse

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


@pytest.fixture()
def project(monkeypatch, tmp_path):
    """A project dir, a configured URL, and every transport verb refusing to
    connect — the shape of `dst dev` not running."""
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DST_URL", "http://localhost:9999")
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def refuse(method: str):
        def _refuse(url, **_kw):
            raise httpx.ConnectError(
                "[Errno 61] Connection refused", request=httpx.Request(method, url)
            )

        return _refuse

    for name in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, name, refuse(name.upper()))
    return tmp_path


# One representative invocation per server-bound verb. The catch is central, so
# this is a sample of the surface rather than a list the fix depends on.
VERBS = [
    ["plan", "--token", "dstadm_t"],
    ["apply", "--token", "dstadm_t"],
    ["export", "--token", "dstadm_t"],
    ["query", "sales", "how many?", "--token", "dstadm_t"],
    ["define", "value", "--token", "dstadm_t"],
    ["sql", "SELECT 1", "--connection", "wh", "--token", "dstadm_t"],
    ["introspect", "--connection", "wh", "--token", "dstadm_t"],
    ["reviews", "--token", "dstadm_t"],
    ["patches", "list", "--lens", "sales", "--token", "dstadm_t"],
    ["keys", "list", "--token", "dstadm_t"],
    ["lens", "rm", "sales", "--yes", "--token", "dstadm_t"],
    ["semantic", "rm", "definition", "value", "--token", "dstadm_t"],
]


@pytest.mark.parametrize("argv", VERBS, ids=lambda a: a[0])
def test_no_server_costs_one_line_on_every_verb(monkeypatch, project, capsys, argv) -> None:
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1, err
    assert "could not reach a dst server at http://localhost:9999" in err
    assert "Connection refused" in err  # the cause
    assert "dst dev" in err and "DST_URL" in err  # what to check


def test_a_slow_server_is_not_reported_as_an_absent_one(monkeypatch, project, capsys) -> None:
    """query and sql never caught ReadTimeout either (HANDS-ON-FINDINGS.md:800).
    The server WAS reached, so the line must not say it could not be."""

    def timeout(url, **_kw):
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", timeout)
    assert _run_cli(monkeypatch, ["query", "sales", "how many?", "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "did not answer in time" in err and "--timeout" in err
    assert "could not reach" not in err


def test_a_failure_to_another_host_is_not_blamed_on_the_server(monkeypatch, capsys) -> None:
    """In-process verbs reach model providers over httpx too. Only a failure to
    the URL this command resolved gets the "is a dst running?" advice — the
    rest are reported as what they are."""
    from services.cli.main import _unreachable_exit

    args = argparse.Namespace(url="http://localhost:9999", url_source="--url")
    exc = httpx.ConnectError(
        "[Errno 61] Connection refused",
        request=httpx.Request("POST", "https://api.provider.example/v1/chat"),
    )
    assert _unreachable_exit(exc, args) == 1
    err = capsys.readouterr().err
    assert "api.provider.example" in err and "could not reach a dst server" not in err


def test_anything_that_is_not_a_transport_failure_still_raises(monkeypatch, project) -> None:
    """The dispatch catch is narrow on purpose: a bug in a verb must keep its
    traceback, or the next one is invisible."""
    monkeypatch.setattr(
        httpx, "post", lambda url, **_kw: (_ for _ in ()).throw(ValueError("a real bug"))
    )
    with pytest.raises(ValueError, match="a real bug"):
        _run_cli(monkeypatch, ["plan", "--token", "dstadm_t"])
