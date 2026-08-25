"""Credential resolution fails loud and names the mistake.

An `@/path` ref that cannot be read must not degrade to None — that is
indistinguishable from an unset secret. A bare service-account path (GCP muscle
memory) must not surface as a JSON parser's internal state."""

from __future__ import annotations

import pytest

from services.config import EnvRefError, resolve_env_ref
from services.project.apply import _bare_path_hint


def test_at_ref_to_a_missing_file_raises_naming_var_and_path(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "typo.json"
    monkeypatch.setenv("DST_PROBE_AT_REF", f"@{missing}")
    with pytest.raises(EnvRefError) as ei:
        resolve_env_ref("DST_PROBE_AT_REF")
    assert "DST_PROBE_AT_REF" in str(ei.value)
    assert str(missing.resolve()) in str(ei.value)


def test_at_ref_to_a_directory_raises_not_oserror(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DST_PROBE_AT_REF", f"@{tmp_path}")
    with pytest.raises(EnvRefError):
        resolve_env_ref("DST_PROBE_AT_REF")


def test_at_ref_to_a_readable_file_resolves(monkeypatch, tmp_path) -> None:
    f = tmp_path / "sa.json"
    f.write_text('{"k": "v"}\n', encoding="utf-8")
    monkeypatch.setenv("DST_PROBE_AT_REF", f"@{f}")
    assert resolve_env_ref("DST_PROBE_AT_REF") == '{"k": "v"}'


def test_dotenv_values_shed_matched_surrounding_quotes(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text('DST_PROBE_QUOTED="/a path/with spaces"\n', encoding="utf-8")
    monkeypatch.delenv("DST_PROBE_QUOTED", raising=False)
    assert resolve_env_ref("DST_PROBE_QUOTED", dirs=[tmp_path]) == "/a path/with spaces"


def test_dotenv_unquoted_values_stay_literal(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("DST_PROBE_LITERAL=/a path/plain\n", encoding="utf-8")
    monkeypatch.delenv("DST_PROBE_LITERAL", raising=False)
    assert resolve_env_ref("DST_PROBE_LITERAL", dirs=[tmp_path]) == "/a path/plain"


def test_bare_path_hint_names_the_at_form(tmp_path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}", encoding="utf-8")
    hint = _bare_path_hint(str(sa), "DST_API_KEY_BIGQUERY")
    assert f"DST_API_KEY_BIGQUERY=@{sa}" in hint


def test_bare_path_hint_stays_silent_for_real_secrets(tmp_path) -> None:
    assert _bare_path_hint('{"type": "service_account"}', "DST_API_KEY_BIGQUERY") == ""
    assert _bare_path_hint(str(tmp_path / "nope.json"), "DST_API_KEY_BIGQUERY") == ""
    assert _bare_path_hint(None, "DST_API_KEY_BIGQUERY") == ""
