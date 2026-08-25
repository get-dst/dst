"""Settings answer to DST_<NAME>; the bare names still work, and say so once.

dst installs from PyPI onto machines whose shells belong to somebody else. With
no prefix its settings claimed `ENVIRONMENT`, `SECRET_KEY`, `EDITION`, `PROVIDERS`,
`PROJECT_FILE` — and that was not hypothetical: an ambient `ENVIRONMENT=production`
in a contributor's shell reached `_production_dsn_ssl` at import, rewrote both DSNs,
and killed the whole suite at collection.

The compatibility half is the part that can rot silently, so it is pinned here: a
deploy that sets only the old name keeps working, gets told what to rename, and loses
to the new name whenever both are set.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import AliasChoices

from services.config import (
    _FIRST_CLASS_BARE,
    Settings,
    legacy_env_names,
    warn_legacy_env_names,
)


def test_every_setting_reads_a_dst_prefixed_name() -> None:
    """The derivation, not a list: a field added tomorrow is prefixed tomorrow. The
    alias generator is what guarantees it, and this is what fails if it is dropped."""
    unprefixed = {
        name
        for name, info in Settings.model_fields.items()
        if not isinstance(info.validation_alias, AliasChoices)
        or not str(info.validation_alias.choices[0]).startswith("DST_")
    }
    assert unprefixed == set()


def test_the_prefixed_name_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence is the whole compatibility contract: an operator mid-rename has both
    spellings in flight, and the one they just added must be the one that steers."""
    monkeypatch.setenv("ENVIRONMENT", "from_the_bare_name")
    assert Settings(_env_file=None).environment == "from_the_bare_name"
    monkeypatch.setenv("DST_ENVIRONMENT", "from_the_prefixed_name")
    assert Settings(_env_file=None).environment == "from_the_prefixed_name"


def test_a_legacy_dotenv_still_configures_the_install(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade path that must not break: a .env written before the rename. Same
    precedence inside the file, so a half-migrated .env behaves like a half-migrated
    shell rather than like a coin flip."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    legacy = tmp_path / "legacy.env"
    legacy.write_text("EDITION=cloud\nSERVING_TIMEOUT_S=7\n", encoding="utf-8")
    settings = Settings(_env_file=str(legacy))
    assert (settings.edition, settings.serving_timeout_s) == ("cloud", 7)

    both = tmp_path / "both.env"
    both.write_text("EDITION=cloud\nDST_EDITION=oss\n", encoding="utf-8")
    assert Settings(_env_file=str(both)).edition == "oss"


def test_the_exceptions_are_the_conventions_we_chose_to_honour() -> None:
    """Reasoned per name, not blanket. DATABASE_URL is injected by every PaaS under
    that exact name and CLERK_* are Clerk's own — answering to them is correct, so they
    are exempt from deprecation. Pinned so the exemption stays a decision and not a
    place things drift into."""
    assert "DATABASE_URL" in _FIRST_CLASS_BARE
    assert "CLERK_SECRET_KEY" in _FIRST_CLASS_BARE
    assert not (_FIRST_CLASS_BARE & set(legacy_env_names()))
    # …and an exempt name still gets a prefixed override, for the deploy whose
    # platform injects a DATABASE_URL pointing at the wrong database.
    choices = Settings.model_fields["database_url"].validation_alias
    assert isinstance(choices, AliasChoices)
    assert [str(c) for c in choices.choices] == ["DST_DATABASE_URL", "DATABASE_URL"]


def test_the_deprecated_names_are_the_ones_that_used_to_be_bare() -> None:
    """The map is derived from the field declarations, so it cannot drift from what the
    model actually reads. Spot-check the names that caused real damage."""
    legacy = legacy_env_names()
    assert legacy["ENVIRONMENT"] == "DST_ENVIRONMENT"
    assert legacy["SECRET_KEY"] == "DST_SECRET_KEY"
    assert legacy["PROVIDERS"] == "DST_PROVIDERS"
    assert all(new == f"DST_{old}" for old, new in legacy.items())


@pytest.fixture()
def no_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/conftest.py declares PROJECT_FILE and DUCKDB_JAFFLE_PATH in their legacy
    spelling on purpose (so every run exercises the aliases). The two tests below are
    about what the warning reports, so they start from nothing planted but their own."""
    for name in legacy_env_names():
        monkeypatch.delenv(name, raising=False)


def test_a_bare_name_logs_one_line_naming_its_replacement(
    no_legacy_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One line, not one per read and not one per variable — a deprecation an operator
    scrolls past is the same as no deprecation."""
    monkeypatch.setenv("EDITION", "cloud")
    monkeypatch.setenv("SERVING_TIMEOUT_S", "5")
    with caplog.at_level(logging.WARNING, logger="dst"):
        warn_legacy_env_names()
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "EDITION (use DST_EDITION)" in message
    assert "SERVING_TIMEOUT_S (use DST_SERVING_TIMEOUT_S)" in message


def test_nothing_is_deprecated_when_the_prefixed_name_is_the_one_in_use(
    no_legacy_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence for the migrated install — including the one still carrying the old name
    beside the new one, where the old name steers nothing."""
    monkeypatch.setenv("DST_EDITION", "cloud")
    monkeypatch.setenv("EDITION", "stale")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    with caplog.at_level(logging.WARNING, logger="dst"):
        warn_legacy_env_names()
    assert caplog.records == []
