"""dst.yaml — parse, secret hygiene, and env-over-file precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.config import ProviderConfig, settings
from services.llm import registry
from services.observability.cost import ai_cost_usd
from services.project.schema import parse_project_yaml

_YAML = """
name: acme-analytics
providers:
  cheapco:
    type: openai-compatible
    base_url: https://api.provider.example
    api_key_env: CHEAPCO_KEY
    fast_model: cheap-flash
    smart_model: cheap-pro
ai_pricing:
  cheap-flash: [0.1, 0.2]
connections:
  warehouse:
    type: snowflake
    config: {account: acme-x1}
    secret_env: SNOWFLAKE_PASSWORD
"""


def test_parses_a_full_project() -> None:
    cfg = parse_project_yaml(_YAML)
    assert cfg.name == "acme-analytics"
    assert cfg.providers["cheapco"].api_key_env == "CHEAPCO_KEY"
    assert cfg.connections["warehouse"].secret_env == "SNOWFLAKE_PASSWORD"
    assert cfg.ai_pricing["cheap-flash"] == (0.1, 0.2)


def test_inline_provider_secret_is_a_parse_error() -> None:
    with pytest.raises(ValueError, match="api_key_env"):
        parse_project_yaml("providers:\n  x:\n    type: anthropic\n    api_key: sk-oops\n")


def test_inline_connection_secret_is_structurally_impossible() -> None:
    with pytest.raises(ValueError):
        parse_project_yaml("connections:\n  wh:\n    type: postgres\n    secret: hunter2\n")


def test_unknown_top_level_keys_rejected() -> None:
    with pytest.raises(ValueError):
        parse_project_yaml("lenses: {}\n")


def _write_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    f = tmp_path / "dst.yaml"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(settings, "project_file", str(f))


def test_registry_reads_the_project_file_when_env_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "providers", {})
    monkeypatch.setenv("CHEAPCO_KEY", "sk-c")
    _write_project(tmp_path, monkeypatch, _YAML)
    assert registry.tier("fast") == "cheapco/cheap-flash"
    assert registry.resolve("cheapco/cheap-pro") is not None
    # And the project's pricing merges in.
    assert ai_cost_usd("cheap-flash", 1_000_000, 1_000_000) == pytest.approx(0.3)


def test_env_providers_win_over_the_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project(tmp_path, monkeypatch, _YAML)
    monkeypatch.setattr(
        settings, "providers", {"anthropic": ProviderConfig(type="anthropic", api_key="sk-a")}
    )
    assert registry.tier("fast") == "anthropic/claude-haiku-4-5"
    assert registry.resolve("cheapco/cheap-pro") is None


def test_a_broken_project_file_is_ignored_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "providers", {})
    _write_project(tmp_path, monkeypatch, "providers: [not, a, mapping]\n")
    assert registry.tier("fast") == ""
    assert registry.resolve("anything") is None
