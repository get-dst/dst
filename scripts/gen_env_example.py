"""Regenerate .env.example from services.config.Settings so it can't go stale.

Run: uv run python -m scripts.gen_env_example
Gate: uv run python -m scripts.gen_env_example --check  (exit 1 if it drifted)

"Generated, therefore current" was a claim nobody checked: a setting could be
added and the file simply not regenerated, which is how the privacy toggle went
missing from the file security docs tell reviewers to read. The --check mode
makes the claim a gate — `make ci` runs it, so the file cannot lag the code.
"""

from __future__ import annotations

import sys
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


OUT = Path(__file__).resolve().parents[1] / ".env.example"


def render() -> str:
    lines = [HEADER]
    for name, field in Settings.model_fields.items():
        default = field.get_default(call_default_factory=True)
        value = "" if default is None or default == {} or default == [] else str(default)
        lines.append(f"{env_name(name, field.validation_alias)}={value}")
    return "\n".join(lines) + "\n"


def main() -> int:
    want = render()
    if "--check" in sys.argv:
        have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if have == want:
            print(f"{OUT.name}: current")
            return 0
        missing = sorted(set(want.splitlines()) - set(have.splitlines()))
        print(
            f"{OUT.name} has drifted from services/config.py. "
            f"Run: uv run python -m scripts.gen_env_example\n"
            + "\n".join(f"  expected: {line}" for line in missing),
            file=sys.stderr,
        )
        return 1
    OUT.write_text(want, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
