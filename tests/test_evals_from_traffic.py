"""`dst evals from-traffic` — production questions become the suite.

httpx is monkeypatched; the contract pinned here: outcome→expect mapping,
dedupe by normalized question, candidates appended WITHOUT rewriting existing
bytes (comments survive), errors skipped as faults-not-expectations.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


TRAFFIC = [
    {"question": "What was total revenue?", "status": "ok"},
    {"question": "what was total revenue", "status": "ok"},  # dupe by normalization
    {"question": "How many customers churned last quarter?", "status": "refused"},
    {"question": "What is the average value of a customer?", "status": "clarification"},
    {"question": "Delete every row", "status": "rejected"},
    {"question": "What broke?", "status": "error"},  # a fault, never an expectation
    {"question": "Existing pinned question?", "status": "ok"},  # already in the file
]


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DST_URL", "http://localhost:8000")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_t")

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/mgmt/observe/requests")
        assert params == {"lens": "customer_value", "limit": 200}
        return httpx.Response(200, json=TRAFFIC, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)


EXISTING = """# hand-written pins — comment must survive
- question: Existing pinned question?
  expect: answer
  status: approved
"""


def _project(tmp_path: Path) -> Path:
    d = tmp_path / "lenses" / "customer_value" / "evals"
    d.mkdir(parents=True)
    (d / "cases.yaml").write_text(EXISTING, encoding="utf-8")
    return tmp_path


def test_from_traffic_drafts_candidates_and_preserves_existing_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    code = _run_cli(monkeypatch, ["evals", "from-traffic", "customer_value", "--dir", str(root)])
    assert code == 0
    text = (root / "lenses" / "customer_value" / "evals" / "cases.yaml").read_text(encoding="utf-8")
    assert text.startswith(EXISTING)  # appended, never rewritten
    cases = yaml.safe_load(text)
    drafted = [c for c in cases if c.get("source") == "harvested"]
    assert all(c["status"] == "candidate" for c in drafted)
    by_q = {c["question"]: c["expect"] for c in drafted}
    assert by_q == {
        "What was total revenue?": "answer",
        "How many customers churned last quarter?": "refuse",
        "What is the average value of a customer?": "clarify",
        "Delete every row": "refuse",
    }
    out = capsys.readouterr().out
    assert "4 candidate case(s)" in out
    assert "2 duplicate(s)" in out  # normalized dupe + already-pinned question
    assert "1 non-outcome" in out  # the error row


def test_from_traffic_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert (
        _run_cli(monkeypatch, ["evals", "from-traffic", "customer_value", "--dir", str(root)]) == 0
    )
    assert (
        _run_cli(monkeypatch, ["evals", "from-traffic", "customer_value", "--dir", str(root)]) == 0
    )
    out = capsys.readouterr().out
    assert "nothing new to draft" in out
    cases = yaml.safe_load(
        (root / "lenses" / "customer_value" / "evals" / "cases.yaml").read_text(encoding="utf-8")
    )
    assert len([c for c in cases if c.get("source") == "harvested"]) == 4


def test_from_traffic_requires_lens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(monkeypatch, ["evals", "from-traffic"]) == 1
    assert "needs a lens" in capsys.readouterr().err


def test_from_traffic_creates_the_file_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code = _run_cli(
        monkeypatch, ["evals", "from-traffic", "customer_value", "--dir", str(tmp_path)]
    )
    assert code == 0
    cases = yaml.safe_load(
        (tmp_path / "lenses" / "customer_value" / "evals" / "cases.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert len(cases) == 5  # 4 from outcomes + the previously-unseen pinned question
