"""The public demo page's template and the setup snippets its mint hands out.

No database: the page is rendered from its inputs (services/api/demo.py
`render_page`) and the snippets are pure. The database halves (apply stores the
`demo:` section, /demo shows it, the mint returns the snippets) live in
test_demo_mode.py.
"""

from __future__ import annotations

import json
import re

import pytest

from services.api.demo import (
    TAGLINE,
    connect_snippets,
    other_snippets,
    render_page,
)
from services.project.schema import DemoConfig, parse_project_yaml

DOTA = """
name: dota
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
        as of: 26 Sep 2026
"""

ASKS = [
    ("Dota 2 pro meta", "How many pro matches were played this week?"),
    ("Dota 2 pro drafts", "Which heroes were the most contested?"),
    ("Dota 2 itemization", "What do pros finish first on Juggernaut?"),
    ("Dota 2 pro playstyle", None),
    ("Dota 2 pro players", "Which carry heroes have the highest win rate?"),
    ("Dota 2 pub meta", "How long is an average ranked game at Divine?"),
]

# The owner's copy rule: the page is for the people the demo serves, and the
# product's own vocabulary on it reads as jargon at best and a worry at worst.
ENGINEER_WORDS = re.compile(
    r"\b(sql|warehouse|governed|semantic layer|receipts?|typed|lens(es)?|entit(y|ies)|"
    r"verification|resolution)\b",
    re.IGNORECASE,
)


def _page(cfg: DemoConfig | None, monkeypatch: pytest.MonkeyPatch, name: str = "roshan") -> str:
    monkeypatch.setenv("DST_INSTANCE_NAME", name)
    return render_page(cfg, ASKS, publishable_key="pk_test_x", frontend_host="ex.clerk.dev")


def _visible(page: str) -> str:
    """The words a visitor can read: no script, no style, no tags."""
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def test_the_example_renders_as_the_visitors_own_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = parse_project_yaml(DOTA).demo
    page = _page(cfg, monkeypatch)
    assert '<p class="lede">Ask about a year of pro Dota 2 from Claude' in page
    chat = page[page.index('<div class="chat"') :]
    assert '<div class="u">I want to learn a carry this patch. What&#x27;s strong?</div>' in chat
    assert '<div class="tool">asked roshan — which carry heroes win their lane' in chat
    assert '<div class="a">Shadow Fiend stands out' in chat
    # receipt lines keep the order the project wrote them in
    lines = re.findall(r"<span><b>([^<]+)</b> · ", chat)
    assert lines == ["lane win", "scope", "as of"]
    # folded, and only shown once the visitor has signed in
    assert '<details class="more example" id="example" hidden>' in page
    assert "See an example conversation" in page
    fine = page[page.index('<p class="fine">') :]
    assert "for 30 days" in fine
    assert '<a href="https://www.example.com/privacy/">Privacy</a>' in fine
    assert "answers by dst (data serve tool)" in fine


def test_without_a_demo_section_the_page_says_only_what_dst_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page(None, monkeypatch)
    assert f'<p class="lede">{TAGLINE}</p>' in page
    assert 'class="chat"' not in page and 'id="example"' not in page
    assert "Privacy</a>" not in page
    assert "Questions are logged with your sign-in email and reviewed" in page
    # the instance called dst needs no credit to itself
    assert "answers by dst" not in _page(None, monkeypatch, name="dst")


def test_what_you_can_ask_is_one_line_per_topic_and_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page(None, monkeypatch)
    box = page[page.index('<section class="asks-box">') :]
    shown, folded = box.split('<details class="more">', 1)
    assert shown.count("<li>") == 4
    assert '<span class="ln">Dota 2 pro meta</span>' in shown
    assert '<span class="q">How many pro matches were played this week?</span>' in shown
    assert "<summary>2 more</summary>" in folded and folded.count("<li>") == 2


def test_the_page_speaks_to_the_people_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """No product vocabulary a visitor can read, before or after sign-in: the
    page text, every setup step and note the mint hands the page, and the
    sign-in page an AI client sends them to."""
    from services.api.oauth import _clerk_consent_html

    for cfg in (None, parse_project_yaml(DOTA).demo):
        assert ENGINEER_WORDS.findall(_visible(_page(cfg, monkeypatch))) == []
    for item in connect_snippets("https://d.example", "roshan", "dst_k", "Who wins?"):
        prose = " ".join([*item["steps"], str(item.get("note", ""))])
        assert ENGINEER_WORDS.findall(prose) == [], item["id"]
    consent = _clerk_consent_html({"client_id": "c"}, "pk", "host", "Claude")
    assert ENGINEER_WORDS.findall(_visible(consent)) == []


def test_project_text_is_never_read_as_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slots fill in one pass: a project that writes a slot's name gets the
    words, not the page's script a second time."""
    cfg = DemoConfig.model_validate({"tagline": "__SCRIPT__ and __NAME__"})
    page = _page(cfg, monkeypatch)
    assert '<p class="lede">__SCRIPT__ and __NAME__</p>' in page
    assert page.count("clerk.browser.js") == 1


def test_each_ai_gets_its_documented_setup() -> None:
    """The shapes each vendor documents for adding a remote MCP server (the doc
    link rides on every entry); the key lands where the client reads a key, and
    the clients that take no static key get the URL for their OAuth sign-in."""
    items = {
        i["id"]: i
        for i in connect_snippets("https://d.example", "roshan", "dst_k", "Who wins lane?")
    }
    assert list(items) == ["claude", "claude-code", "codex", "chatgpt", "cursor"]
    assert all(str(i["doc"]).startswith("https://") for i in items.values())
    assert items["claude"]["code"] == "https://d.example/mcp"
    assert items["chatgpt"]["code"] == "https://d.example/mcp"
    assert items["claude-code"]["code"] == (
        "claude mcp add --transport http roshan https://d.example/mcp \\\n"
        '  --header "Authorization: Bearer dst_k"'
    )
    assert items["codex"]["code"] == (
        "export ROSHAN_API_KEY=dst_k\n"
        "codex mcp add roshan --url https://d.example/mcp --bearer-token-env-var ROSHAN_API_KEY"
    )
    assert json.loads(str(items["cursor"]["code"])) == {
        "mcpServers": {
            "roshan": {
                "url": "https://d.example/mcp",
                "headers": {"Authorization": "Bearer dst_k"},
            }
        }
    }
    assert "`ask roshan: Who wins lane?`" in items["claude"]["steps"][-1]
    # a name no client config accepts as a key is made into one
    odd = connect_snippets("https://d.example", "Roshan Demo!", "dst_k", None)
    assert "codex mcp add roshan-demo --url" in str(odd[2]["code"])


def test_the_other_ways_in_quote_the_question_safely() -> None:
    curl, openai = other_snippets("https://d.example", "dst_k", "drafts", "Who's first?")
    assert curl["code"] == (
        "curl -s https://d.example/v1/query \\\n"
        "  -H 'Authorization: Bearer dst_k' -H 'Content-Type: application/json' \\\n"
        """  -d '{"q": "Who'"'"'s first?"}'"""
    )
    assert 'model="dst/drafts"' in str(openai["code"])
    assert '"content": "Who\'s first?"' in str(openai["code"])
