from fastapi.testclient import TestClient

from services import __version__
from services.app import app

client = TestClient(app)


def test_health() -> None:
    from services import build_info

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["edition"] == "oss" and body["version"] == __version__
    # The running build is identifiable — /health serves exactly what
    # startup captured (a SHA + dirty flag in a checkout, nulls in a wheel).
    assert body["git_sha"] == build_info.GIT_SHA
    assert body["git_dirty"] == build_info.GIT_DIRTY
    # Never "dev" in an installed tree: the deploy contract pins by version.
    assert __version__ != "dev"


def test_build_identity_captured_in_this_checkout() -> None:
    # The suite runs in a git checkout (worktrees included — .git is a file
    # there): capture must yield a real SHA and a real flag, not nulls.
    from services import build_info

    sha, dirty = build_info._capture()
    assert sha is not None and len(sha) == 40
    assert isinstance(dirty, bool)


def test_build_identity_null_without_git_dir(monkeypatch, tmp_path) -> None:
    # Packaged installs have no .git next to the package — identity is null,
    # never an error (and a venv inside a FOREIGN repo must not borrow its SHA).
    from services import build_info

    monkeypatch.setattr(build_info, "__file__", str(tmp_path / "pkg" / "build_info.py"))
    assert build_info._capture() == (None, None)


def test_build_identity_null_when_git_fails(monkeypatch, tmp_path) -> None:
    # Zero startup failure modes: a missing/hanging git binary degrades to null.
    from services import build_info

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "__file__", str(tmp_path / "pkg" / "build_info.py"))

    def boom(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("no git on PATH")

    monkeypatch.setattr(build_info.subprocess, "run", boom)
    assert build_info._capture() == (None, None)


def test_ready(live_client: TestClient) -> None:
    # Uses the lifespan-running client (see conftest): /ready now probes the MCP session
    # manager too, which is only live under the lifespan.
    resp = live_client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["mcp"] == "ok"
