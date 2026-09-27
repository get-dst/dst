"""dst.yaml's `demo:` section: what a public demo's page shows besides what dst derives.

Validated through the same parse `dst plan` and `dst apply` run on dst.yaml, so a
malformed section fails both, naming the key. Storing it and rendering it are
pinned in test_demo_mode.py and test_demo_page.py.
"""

from __future__ import annotations

import pytest

from services.project.schema import parse_project_yaml

DOTA = """
demo:
  tagline: Ask about a year of pro Dota 2 from Claude, ChatGPT or any AI you use.
  privacy_url: https://www.example.com/privacy/
  log_days: 30
  example:
    - user: I want to learn a carry this patch. What's strong?
    - tool: asked roshan — which carry heroes win their lane most often?
    - assistant: |
        Shadow Fiend stands out: pro carries on him win their lane 59.1% of the time.
    - receipt:
        lane win: 400 gold ahead at minute 10
        scope: premium and professional leagues
        games: 12
"""


@pytest.mark.parametrize(
    ("section", "error"),
    [
        ("example: [{usr: hi}]", "unknown key `usr` — did you mean `user`?"),
        ("exmaple: []", "unknown key `exmaple` — did you mean `example`?"),
        ("example: [{}]", "demo.example.0: each turn is exactly one of user, tool, assistant"),
        ("example: [{user: hi, tool: x}]", "this one has user, tool"),
        ('example: [{assistant: "  "}]', "an empty `assistant` turn"),
        ("example: [{receipt: {}}]", "an empty `receipt` turn"),
        ("example: [hello]", "demo.example.0"),
        ("privacy_url: javascript:alert(1)", "privacy_url must be an http(s) URL"),
        ("log_days: 0", "demo.log_days"),
    ],
)
def test_a_malformed_demo_section_is_a_named_error(section: str, error: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_project_yaml(f"demo:\n  {section}")
    assert error in str(exc.value)
    assert str(exc.value).startswith("dst.yaml: demo")


def test_a_demo_section_is_optional_and_every_key_in_it_too() -> None:
    assert parse_project_yaml("name: t").demo is None
    empty = parse_project_yaml("demo: {}").demo
    assert empty is not None and empty.example == [] and empty.tagline is None
    full = parse_project_yaml(DOTA).demo
    assert full is not None and len(full.example) == 4
    assert full.log_days == 30
    # a number a person wrote as a receipt value is text on the page
    assert full.example[3].receipt == {
        "lane win": "400 gold ahead at minute 10",
        "scope": "premium and professional leagues",
        "games": "12",
    }
