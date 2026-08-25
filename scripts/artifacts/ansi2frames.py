"""ANSI terminal captures -> dst-styled HTML frames + standalone SVGs.

Reads cap/*.ansi (raw pty captures of the dst CLI), parses the SGR codes the
CLI actually emits (style.py: bold, dim, 31/32/33, 38;5;172), and renders:
  - dst-terminal-artifacts.html  (the artifact page, real output)
  - svg/<name>.svg               (self-contained frames for README embedding)
"""
# ruff: noqa: E501

import html
import re
from pathlib import Path

BASE = Path(__file__).parent
CAP = BASE / "cap"
SVG_DIR = BASE.parent.parent / "docs" / "oss" / "docs" / "assets" / "term"
SVG_DIR.mkdir(exist_ok=True)

# terminal palette (dark frame is theme-invariant)
COLORS = {
    "green": "#7cbd82",
    "yellow": "#d9b45b",
    "red": "#e0736a",
    "amber": "#d78700",
    "dim": "#857b6f",
    "ink": "#d9d2c5",
    "bright": "#efe9dd",
}

SGR = re.compile(r"\x1b\[([0-9;]*)m")


def parse(raw: str):
    """-> list of lines, each a list of (text, color, bold, dim) runs."""
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")
    txt = re.sub(r"[\x04\x08]", "", txt)
    txt = re.sub(r"\^D", "", txt, count=2)  # macOS `script` echoes the EOF marker in caret notation
    txt = re.sub(r"\x1b\[[0-9;]*[A-LN-Za-ln-z]", "", txt)  # non-SGR CSI
    lines, cur, runs = [], [], []
    color, bold, dim = None, False, False
    pos = 0
    for m in SGR.finditer(txt):
        seg = txt[pos : m.start()]
        pos = m.end()
        if seg:
            runs.append((seg, color, bold, dim))
        params = [p for p in m.group(1).split(";") if p != ""] or ["0"]
        i = 0
        while i < len(params):
            p = params[i]
            if p == "0":
                color, bold, dim = None, False, False
            elif p == "1":
                bold = True
            elif p == "2":
                dim = True
            elif p == "22":
                bold = dim = False
            elif p == "31":
                color = "red"
            elif p == "32":
                color = "green"
            elif p == "33":
                color = "yellow"
            elif p == "38" and params[i : i + 3] == ["38", "5", "172"]:
                color = "amber"
                i += 2
            elif p == "39":
                color = None
            i += 1
    if txt[pos:]:
        runs.append((txt[pos:], color, bold, dim))

    for text, c, b, d in runs:
        parts = text.split("\n")
        for j, part in enumerate(parts):
            if j > 0:
                lines.append(cur)
                cur = []
            if part:
                cur.append((part, c, b, d))
    if cur:
        lines.append(cur)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


WRAP = 100  # faithful hard-wrap: what a 100-column terminal displays


def wrap_lines(lines, width=WRAP):
    out = []
    for line in lines:
        cur, col = [], 0
        for text, c, b, d in line:
            while text:
                room = width - col
                if len(text) <= room:
                    cur.append((text, c, b, d))
                    col += len(text)
                    text = ""
                else:
                    cur.append((text[:room], c, b, d))
                    out.append(cur)
                    cur, col = [], 0
                    text = text[room:]
        out.append(cur)
    return out


def run_html(text, c, b, d):
    cls = " ".join(x for x in (f"c-{c}" if c else "", "bold" if b else "", "dim" if d else "") if x)
    esc = html.escape(text)
    return f'<span class="{cls}">{esc}</span>' if cls else esc


def frame_html(name, cmd, caption):
    lines = wrap_lines(parse((CAP / f"{name}.ansi").read_text()))
    body = f'<span class="p">$</span> <span class="bold">{html.escape(cmd)}</span>\n'
    body += "\n".join("".join(run_html(*r) for r in line) for line in lines)
    return f"""  <figure>
    <div class="term">
      <div class="bar"><span class="cmd">{html.escape(cmd)}</span><span>~/demo · zsh</span></div>
      <pre>{body}</pre>
    </div>
    <figcaption>{caption}</figcaption>
  </figure>"""


def frame_svg(name, cmd):
    lines = wrap_lines(parse((CAP / f"{name}.ansi").read_text()))
    all_lines = [[(f"$ {cmd}", "prompt", False, False)]] + lines
    width = max(20, max(sum(len(t) for t, *_ in ln) for ln in all_lines if ln))
    w = round(width * 7.85) + 48
    h = len(all_lines) * 21 + 66
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="13">',
        f'<rect width="{w}" height="{h}" rx="8" fill="#14100d" stroke="#3a332c"/>',
        f'<text x="20" y="24" fill="#857b6f" font-size="11">{html.escape(cmd)}</text>',
        f'<line x1="0" y1="36" x2="{w}" y2="36" stroke="#3a332c"/>',
    ]
    y = 60
    for ln in all_lines:
        if ln:
            spans = []
            for text, c, b, d in ln:
                if c == "prompt":
                    fill, weight = COLORS["dim"], "normal"
                    esc = html.escape(text)
                    spans.append(
                        f'<tspan fill="{fill}">$</tspan>'
                        f'<tspan fill="{COLORS["bright"]}" font-weight="700">{esc[1:]}</tspan>'
                    )
                    continue
                fill = COLORS.get(c) if c else (COLORS["dim"] if d else COLORS["ink"])
                if b and not c:
                    fill = COLORS["bright"]
                weight = ' font-weight="700"' if b else ""
                spans.append(f'<tspan fill="{fill}"{weight}>{html.escape(text)}</tspan>')
            out.append(f'<text x="20" y="{y}" xml:space="preserve">{"".join(spans)}</text>')
        y += 21
    out.append("</svg>")
    (SVG_DIR / f"{name}.svg").write_text("\n".join(out))


FRAMES = [
    (
        "plan1",
        "dst plan",
        "<b>day one · plan</b>, cold start — six assets to create, and the block plan cannot "
        "check printed verbatim: apply still probes connections, executes eval oracles, and "
        "runs the publish gate. A plan with invalid files exits 1: plan predicts apply.",
    ),
    (
        "apply",
        "dst apply",
        "<b>day one · apply</b>, first publish — the connection probed and read through, every "
        "asset narrated, and five warnings that earn their lines: the 'value' ambiguity, the "
        "admin-only allow-list, and the loud <b>gate SKIPPED</b> — a fresh lens with nothing "
        "certified publishes, but never silently pretends it was tested.",
    ),
    (
        "test",
        "dst test customer_value",
        "<b>day one · test</b>, before anything is certified — 0/0 exits <b>4</b>, never "
        "green: nothing passed and nothing failed because there was nothing to verify.",
    ),
    (
        "test2",
        "dst test customer_value",
        "<b>day one · test</b>, after certifying two verified answers and approving a clarify "
        "expectation — every certified answer is a regression test: its stored SQL runs as "
        "the oracle, its question re-runs through live generation, executed results compared.",
    ),
    (
        "plan_diff",
        "dst plan --full",
        "<b>day two · plan --full</b> — someone edits what 'repeat customer' means. The plan "
        "shows the definition diff, marks the lens <b>STALE</b> (shared changed), and names "
        "the certified answer the change touches — before anything is deployed.",
    ),
    (
        "apply_blocked",
        "dst apply",
        "<b>day two · apply, blocked</b> — the eval gate re-runs the certified corpus against "
        "live generation under the new definition: <b>certified SQL → 19, generated → 1</b>, "
        "attributed to the definition changed in this push. Accuracy regressed, the lens is "
        "rejected, everything rolls back: <b>nothing deployed</b>. The fork is printed: fix "
        "the definition, or re-certify/retire the answer.",
    ),
    (
        "drift",
        "dst drift --connection jaffle",
        "<b>day N · drift</b> — the warehouse moved: a column appeared, and dst crosses it "
        "with the semantic layer that reads the table. A bare column list is what nobody "
        "reads; the cross-reference is the feature.",
    ),
    (
        "keys",
        "dst keys create --caller maija",
        "<b>governance · keys</b> — one scoped key per person, shown once. What a caller can "
        "reach is decided by lens allow-lists, not by which credential leaked furthest.",
    ),
    (
        "correct",
        'dst correct req_… --kind definition --target revenue --note "…"',
        "<b>flywheel · flag</b> — a wrong answer is flagged against the request that "
        "produced it. The correction names the request, the kind, and the target term.",
    ),
    (
        "patch_draft",
        "dst patches draft rev_81907144f8",
        "<b>flywheel · draft</b> — the AI drafts the amended definition from the ruling. "
        "The instruction is explicit: check the changed ruling, then approve or reject.",
    ),
    (
        "patch_approve",
        "dst patches approve 2094ed1f… --dir .",
        "<b>flywheel · approve</b> — the human approves, never authors; the fix lands as a "
        "file in the repo, <b>not live until commit + apply</b>, and a candidate eval case is "
        "filed so the same class of error cannot ship twice unnoticed.",
    ),
    (
        "observe3",
        "dst observe",
        "<b>observe</b> — the ledger: every request, its AI cost, and its outcome, per "
        "caller. Declines are governed outcomes; errors are faults; they are never one "
        "number.",
    ),
]

CSS = """
  :root {
    --paper: #faf6ee; --ink: #292524; --dim: #8a8178; --line: #ddd4c4; --amber: #b45309;
  }
  @media (prefers-color-scheme: dark) {
    :root { --paper: #171412; --ink: #e8e2d8; --dim: #8f867b; --line: #38312a; --amber: #e8930c; }
  }
  :root[data-theme="light"] { --paper: #faf6ee; --ink: #292524; --dim: #8a8178; --line: #ddd4c4; --amber: #b45309; }
  :root[data-theme="dark"]  { --paper: #171412; --ink: #e8e2d8; --dim: #8f867b; --line: #38312a; --amber: #e8930c; }
  html { background: var(--paper); }
  body {
    background: var(--paper); color: var(--ink);
    font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    line-height: 1.55; padding: 48px 24px 64px;
  }
  .sheet { max-width: 1000px; margin: 0 auto; display: flex; flex-direction: column; gap: 26px; }
  header .eyebrow { color: var(--amber); font-size: 11px; letter-spacing: .14em; text-transform: uppercase; margin-bottom: 10px; }
  header h1 { font-size: 23px; font-weight: 700; margin-bottom: 12px; text-wrap: balance; }
  header .lede { max-width: 70ch; font-size: 13.5px; }
  header .lede em { font-style: normal; color: var(--amber); }
  .term { background: #14100d; border: 1px solid #3a332c; border-radius: 6px; overflow: hidden; }
  .term .bar {
    display: flex; justify-content: space-between; gap: 24px; padding: 8px 16px;
    background: #1d1815; border-bottom: 1px solid #3a332c; font-size: 11px; color: #857b6f;
  }
  .term .bar .cmd { color: #b3a894; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .term pre {
    margin: 0; padding: 18px 22px 20px; font-family: inherit; font-size: 14.5px;
    line-height: 1.6; color: #d9d2c5; overflow-x: auto; font-variant-numeric: tabular-nums;
  }
  .bold { font-weight: 700; color: #efe9dd; }
  .c-green  { color: #7cbd82; }
  .c-yellow { color: #d9b45b; }
  .c-red    { color: #e0736a; }
  .c-amber  { color: #d78700; }
  .dim, .p  { color: #857b6f; }
  .c-green.bold { color: #7cbd82; }
  .c-red.bold   { color: #e0736a; }
  figure { display: flex; flex-direction: column; }
  figcaption { font-size: 12.5px; color: var(--dim); padding-top: 10px; max-width: 86ch; }
  figcaption b { color: var(--amber); font-weight: 700; }
  footer.series { font-size: 11.5px; color: var(--dim); border-top: 1px solid var(--line); padding-top: 14px; }
"""

HEADER = """  <header>
    <div class="eyebrow">data serve tool · terminal artifacts · captured live</div>
    <h1>The deploy loop, for real</h1>
    <p class="lede">Genuine output — a cold <em>dst init</em> demo project (bundled jaffle
    DuckDB warehouse, deepseek-v4 generation, local embeddings), captured over a pty on
    2026-08-24. Nothing staged, nothing edited. Day one is the cold start; day two is what
    the machinery is for: a definition change that breaks a certified answer cannot ship.
    These are the engineer's verbs — caller traffic arrives from your AIs over MCP/REST,
    not from a terminal.</p>
  </header>"""

FOOTER = """  <footer class="series">
    captured: dst 0.2.28 · demo project (dst init --warehouse demo) · deepseek-v4-flash/pro ·
    frames regenerable via ansi2frames.py from cap/*.ansi
  </footer>"""

page = [
    "<title>dst — terminal artifacts</title>",
    f"<style>{CSS}</style>",
    '<div class="sheet">',
    HEADER,
]
for name, cmd, caption in FRAMES:
    page.append(frame_html(name, cmd, caption))
    frame_svg(name, cmd)
page += [FOOTER, "</div>"]
(BASE / "dst-terminal-artifacts.html").write_text("\n".join(page))
print("wrote", BASE / "dst-terminal-artifacts.html")
print("wrote", len(FRAMES), "svgs in", SVG_DIR)
