"""Regenerate .env.example from services.config.Settings so it can't go stale.

Run: uv run python -m scripts.gen_env_example
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices

from services.config import _FIRST_CLASS_BARE, Settings

HEADER = """\
# dst configuration — generated from services/config.py by scripts/gen_env_example.py.
# Do not hand-edit; change Settings and re-run the script.
# Copy to .env and fill in what you need. Blank values are optional at boot.
"""


def env_name(field_name: str, alias: object) -> str:
    """The name to TEACH for this field: normally its ``DST_`` one, but the
    unprefixed name where that is somebody else's convention we honour on purpose
    (``DATABASE_URL``, ``CLERK_*`` — see ``_FIRST_CLASS_BARE``). Printing
    ``DST_CLERK_SECRET_KEY`` beside Clerk's own docs would teach away from the
    very compatibility the exception exists for."""
    if isinstance(alias, AliasChoices):
        choices = [str(choice) for choice in alias.choices]
        return next((c for c in choices if c in _FIRST_CLASS_BARE), choices[0])
    return field_name.upper()


def main() -> None:
    lines = [HEADER]
    for name, field in Settings.model_fields.items():
        default = field.get_default(call_default_factory=True)
        value = "" if default is None or default == {} or default == [] else str(default)
        lines.append(f"{env_name(name, field.validation_alias)}={value}")
    out = Path(__file__).resolve().parents[1] / ".env.example"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
