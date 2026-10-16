"""`dst doctor` — callability, not configuration.

A fully-configured but uncallable provider is otherwise only discoverable via a
real query's 500 and a server-log traceback; doctor makes one cheap real call per
tier and prints the failure verbatim. Pure: registry and schema state are
monkeypatched — no DB, no network."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.contracts.errors import ProviderError


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


@dataclass
class _State:
    status: str = "ok"

    def summary(self) -> str:
        return "ok (0058)"


class _OkLLM:
    def complete(self, **kw: object) -> None:
        return None


class _BrokenLLM:
    def complete(self, **kw: object) -> None:
        raise ProviderError("anthropic", "SDK call signature mismatch — anthropic 1.0.0")


@dataclass
class _Pair:
    llm: object
    name: str


def _wire(monkeypatch, llm: object) -> None:
    from services.db import schema_state as ss
    from services.llm import registry

    monkeypatch.setattr(ss, "schema_state", lambda: _State())
    monkeypatch.setattr(registry, "resolve_embedder", lambda: object())
    monkeypatch.setattr(registry, "tier", lambda name: f"prov/{name}-model")
    monkeypatch.setattr(registry, "resolve", lambda ref: _Pair(llm, ref))


def test_doctor_green_exits_zero(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _wire(monkeypatch, _OkLLM())
    assert _run_cli(monkeypatch, ["doctor"]) == 0
    out = capsys.readouterr().out
    assert "db            ok (0058)" in out
    assert "embeddings    ok" in out
    assert out.count("ok\n") >= 2  # both tiers called and reported


def test_doctor_names_an_uncallable_provider_and_exits_one(monkeypatch, tmp_path, capsys) -> None:
    # Config resolves, the CALL fails — the report
    # names the tier, the model, and the verbatim failure.
    monkeypatch.chdir(tmp_path)
    _wire(monkeypatch, _BrokenLLM())
    assert _run_cli(monkeypatch, ["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL — anthropic: SDK call signature mismatch" in out


def test_doctor_reports_an_unservable_tier_without_calling(monkeypatch, tmp_path, capsys) -> None:
    from services.db import schema_state as ss
    from services.llm import registry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ss, "schema_state", lambda: _State())
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    monkeypatch.setattr(registry, "tier", lambda name: "")
    monkeypatch.setattr(registry, "resolve", lambda ref: None)
    monkeypatch.setattr(registry, "unservable_detail", lambda ref: "no provider configured")
    assert _run_cli(monkeypatch, ["doctor"]) == 0  # unservable is a SKIP, not a failure
    out = capsys.readouterr().out
    assert "certified matching off" in out
    assert "SKIP — no provider configured" in out


@pytest.mark.parametrize("flag", ["--dir"])
def test_doctor_accepts_dir(monkeypatch, tmp_path, flag) -> None:
    _wire(monkeypatch, _OkLLM())
    assert _run_cli(monkeypatch, ["doctor", flag, str(tmp_path)]) == 0


def _project_with_corpus(root, entries: str) -> None:  # noqa: ANN001
    (root / "dst.yaml").write_text("name: p\n", encoding="utf-8")
    lens = root / "lenses" / "cv"
    lens.mkdir(parents=True)
    (lens / "certified_answers.yaml").write_text(entries, encoding="utf-8")


def test_doctor_fails_red_when_a_corpus_exists_without_an_embedder(
    monkeypatch, tmp_path, capsys
) -> None:
    """Certified answers with no embedder store unembedded and never serve:
    a failure of the install, said in red, not a note."""
    from services.cli import style
    from services.db import schema_state as ss
    from services.llm import registry

    monkeypatch.chdir(tmp_path)
    _project_with_corpus(tmp_path, "- question: how many?\n  sql: SELECT 1\n")
    monkeypatch.setattr(ss, "schema_state", lambda: _State())
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    monkeypatch.setattr(registry, "tier", lambda name: "")
    monkeypatch.setattr(registry, "resolve", lambda ref: None)
    monkeypatch.setattr(registry, "unservable_detail", lambda ref: "none")
    monkeypatch.setattr(style, "bad", lambda text: f"<red>{text}</red>")
    assert _run_cli(monkeypatch, ["doctor"]) == 1
    out = capsys.readouterr().out
    assert "<red>embeddings    FAIL" in out
    assert "certified matching is OFF" in out and "lenses/cv/certified_answers.yaml" in out


def test_doctor_treats_a_comment_only_corpus_file_as_no_corpus(
    monkeypatch, tmp_path, capsys
) -> None:
    from services.db import schema_state as ss
    from services.llm import registry

    monkeypatch.chdir(tmp_path)
    _project_with_corpus(tmp_path, "# nothing certified yet\n")
    monkeypatch.setattr(ss, "schema_state", lambda: _State())
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    monkeypatch.setattr(registry, "tier", lambda name: "")
    monkeypatch.setattr(registry, "resolve", lambda ref: None)
    monkeypatch.setattr(registry, "unservable_detail", lambda ref: "none")
    assert _run_cli(monkeypatch, ["doctor"]) == 0
    assert "not configured — certified matching off" in capsys.readouterr().out
