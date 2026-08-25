"""`dst serve`'s port preflight must never be stricter than uvicorn.

The probe used a bare `socket.socket()` while uvicorn sets SO_REUSEADDR before
binding (uvicorn/config.py bind_socket), so a port left in TIME_WAIT by a
just-killed server read as "already in use" even though uvicorn would have
bound it — a false refusal at the one moment it matters, a restart. The
accompanying message interpolated `lsof -ti` output raw, and that output is
newline-separated, so two PIDs shredded the sentence across four lines.

No server and no DB here: the probe socket is inspected directly, the message
is a pure function of the port and the lsof text."""

from __future__ import annotations

import socket

import pytest

from services.cli.main import _port_busy_message, _probe_socket


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_probe_sets_so_reuseaddr(host: str) -> None:
    """Same option uvicorn sets — otherwise the check outranks the server.
    getsockopt reports the stored flag bit, not a normalised 1 (darwin: 4)."""
    with _probe_socket(host) as probe:
        assert probe.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)


def test_probe_family_follows_host() -> None:
    """uvicorn switches to AF_INET6 on a colon-bearing host; an AF_INET probe
    against `--host ::` would fail to bind for the wrong reason entirely."""
    with _probe_socket("127.0.0.1") as v4, _probe_socket("::") as v6:
        assert v4.family is socket.AF_INET
        assert v6.family is socket.AF_INET6


def test_probe_binds_a_port_in_time_wait() -> None:
    """The live regression, reproduced: park a port in TIME_WAIT (the server
    side must close first), then show the old bare probe refuses it while the
    real probe — like uvicorn — takes it."""
    with _probe_socket("127.0.0.1") as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        served, _ = listener.accept()
        served.close()  # active close — (127.0.0.1, port) enters TIME_WAIT
        client.close()

    with socket.socket() as bare, pytest.raises(OSError):
        bare.bind(("127.0.0.1", port))  # what `dst serve` used to do
    with _probe_socket("127.0.0.1") as probe:
        probe.bind(("127.0.0.1", port))  # what uvicorn would have done all along


def test_multi_pid_message_is_space_joined() -> None:
    """`lsof -ti` is one PID per line; raw interpolation broke the sentence."""
    msg = _port_busy_message(8001, "5943\n17909\n")
    assert "by PID 5943 17909 " in msg
    assert "(kill 5943 17909)" in msg
    assert msg.count("\n") == 1  # one deliberate break, not one per PID


def test_single_pid_message_is_unchanged() -> None:
    msg = _port_busy_message(8000, "4242\n")
    assert "by PID 4242 " in msg
    assert "(kill 4242)" in msg


@pytest.mark.parametrize("empty", ["", "\n", "   \n"])
def test_no_pid_message_names_time_wait_and_the_way_out(empty: str) -> None:
    """lsof finding nobody means the port is free of listeners; blaming
    "another dst" sent people hunting a process that does not exist."""
    msg = _port_busy_message(8001, empty)
    assert "PID" not in msg and "another dst" not in msg
    assert "TIME_WAIT" in msg
    assert "Retry" in msg and "--port" in msg
