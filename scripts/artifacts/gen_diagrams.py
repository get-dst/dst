"""dst architecture figures — sparse, datasheet-idiom SVGs on paper ground. v3."""

import html
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE.parent.parent / "docs" / "oss" / "docs" / "assets" / "figures"
OUT.mkdir(exist_ok=True)

PAPER = "#faf6ee"
SURF = "#fffdf7"
INK = "#292524"
DIM = "#8a8178"
LINE = "#d8cfbf"
AMBER = "#b45309"
AMBER_W = "#f6e8d7"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def svg_open(w, h):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'font-family="{MONO}" font-size="13">'
        f"<defs>"
        f'<marker id="ai" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="7.5" '
        f'markerHeight="6.5" orient="auto-start-reverse">'
        f'<polygon points="0,0 10,4 0,8" fill="{INK}"/></marker>'
        f'<marker id="aa" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="7.5" '
        f'markerHeight="6.5" orient="auto-start-reverse">'
        f'<polygon points="0,0 10,4 0,8" fill="{AMBER}"/></marker>'
        f"</defs>"
        f'<rect width="{w}" height="{h}" fill="{PAPER}"/>'
    )


def box(x, y, w, h, stroke=INK, fill=SURF, sw=1.1):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def t(x, y, s, size=13, fill=INK, anchor="start", bold=False):
    fw = ' font-weight="700"' if bold else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}"{fw} xml:space="preserve">{html.escape(s)}</text>'
    )


def line(x1, y1, x2, y2, stroke=INK, arrow=None, sw=1.1, dash=None):
    m = f' marker-end="url(#{arrow})"' if arrow else ""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"{m}{d}/>'


def receipt(x, y, w, h):
    """A receipt: straight sides, zigzag torn bottom edge."""
    # ruff: noqa: E501
    teeth, amp = 14, 7
    pts = [f"M {x} {y}", f"L {x + w} {y}", f"L {x + w} {y + h - amp}"]
    n = w // teeth
    for i in range(int(n)):
        px = x + w - (i + 0.5) * teeth
        pts.append(f"L {px} {y + h}")
        pts.append(f"L {max(x, x + w - (i + 1) * teeth)} {y + h - amp}")
    pts.append("Z")
    return f'<path d="{" ".join(pts)}" fill="{SURF}" stroke="{INK}" stroke-width="1.1"/>'


def write(name, parts):
    (OUT / f"{name}.svg").write_text("\n".join(parts) + "\n</svg>")


# ---- fig 1 · the shape of the system --------------------------------------
s = [svg_open(1020, 480)]
s += [
    box(300, 30, 400, 56),
    t(500, 54, "The AI your team uses", 14, anchor="middle", bold=True),
    t(500, 74, "Claude · ChatGPT · Copilot · your agents", 11, DIM, "middle"),
]
s += [
    line(440, 86, 440, 146, INK, "ai", 1.2),
    t(428, 114, "“how much revenue last month?”", 11.5, INK, "end"),
    line(560, 146, 560, 86, AMBER, "aa", 1.4),
    t(572, 114, "the answer — cited, priced", 11.5, AMBER),
]
s += [
    box(150, 148, 620, 134, AMBER, AMBER_W, 1.6),
    t(174, 176, "dst", 19, AMBER, bold=True),
    t(746, 176, "governed data access layer", 11, DIM, "end"),
]
chips = [
    ("who may ask?", "deny-by-default"),
    ("what do words mean?", "revenue → SUM(amount)"),
    ("which tables?", "orders ⨝ customers"),
    ("answer, composed", "cited · priced"),
]
xs = [174, 320, 466, 612]
for x, (q, sub) in zip(xs, chips, strict=True):
    s += [
        box(x, 200, 132, 46, AMBER, PAPER),
        t(x + 66, 220, q, 11.5, anchor="middle", bold=True),
        t(x + 66, 237, sub, 9.5, DIM, "middle"),
    ]
    if x != 612:
        s.append(line(x + 134, 223, x + 144, 223, AMBER, "aa", 1.2))
s.append(line(770, 223, 812, 223, INK, "ai"))
s.append(receipt(816, 150, 176, 212))
s += [
    t(904, 176, "receipt", 12.5, anchor="middle", bold=True),
    line(830, 188, 978, 188, DIM, dash="3 3"),
    t(830, 210, "question", 10, DIM),
    t(978, 210, "revenue…", 10, INK, "end"),
    t(830, 230, "sql", 10, DIM),
    t(978, 230, "SELECT SUM(…)", 10, INK, "end"),
    t(830, 250, "verdict", 10, DIM),
    t(978, 250, "verified", 10, INK, "end"),
    t(830, 270, "AI cost", 10, DIM),
    t(978, 270, "$0.0008", 10, INK, "end"),
    t(830, 290, "your data", 10, DIM),
    t(978, 290, "2.1 MB read", 10, INK, "end"),
    t(830, 310, "caller", 10, DIM),
    t(978, 310, "sales-agent", 10, INK, "end"),
    line(830, 324, 978, 324, DIM, dash="3 3"),
    t(904, 342, "every call keeps one", 9.5, DIM, "middle"),
]
s.append(line(460, 282, 460, 336, AMBER, "aa", 1.4))
s += [
    box(160, 338, 600, 50),
    t(
        460,
        368,
        "your data — BigQuery · Snowflake · Postgres · MySQL · DuckDB",
        12.5,
        anchor="middle",
    ),
]
write("fig1-shape", s)

# ---- fig 2 · lenses, and a question finding its lens ----------------------
s = [svg_open(980, 470)]
s += [
    box(290, 30, 400, 52),
    t(490, 52, "“how were commissions last quarter?”", 12.5, anchor="middle", bold=True),
    t(490, 71, "from any caller, over MCP or REST", 10.5, DIM, "middle"),
]
s += [
    line(490, 82, 490, 136, AMBER, "aa", 1.4),
    t(506, 112, "routed to the covering lens", 11, AMBER),
]


def lens_card(sx, name, rows, hot=False):
    c = []
    c.append(
        box(sx, 140, 280, 190, AMBER if hot else INK, AMBER_W if hot else SURF, 1.6 if hot else 1.1)
    )
    c.append(t(sx + 20, 168, f"lens — {name}", 13, AMBER if hot else INK, bold=True))
    c.append(line(sx + 20, 180, sx + 260, 180, LINE))
    y = 204
    for k, v in rows:
        c.append(t(sx + 20, y, k, 11, INK if hot else DIM, bold=hot))
        c.append(t(sx + 92, y, v, 11, DIM))
        y += 26
    return c


s += lens_card(
    40,
    "finance_close",
    [
        ("for", "maija · pekka · antti"),
        ("data", "gl · journals"),
        ("means", "revenue, margin"),
        ("proven", "23 certified"),
        ("30d", "212 calls · 99% ok"),
    ],
)
s += lens_card(
    350,
    "sales_comp",
    [
        ("for", "sales · its agent"),
        ("data", "deals · payouts"),
        ("means", "commission, earnings"),
        ("proven", "12 certified"),
        ("30d", "8,910 calls · 98% ok"),
    ],
    hot=True,
)
s += lens_card(
    660,
    "churn_risk",
    [
        ("for", "the cs agent"),
        ("data", "accounts · usage"),
        ("means", "churn, active"),
        ("proven", "8 certified"),
        ("30d", "1,204 calls · 97% ok"),
    ],
)
s.append(
    t(
        490,
        380,
        "same warehouse — each lens selects its own data and its own meanings",
        11,
        DIM,
        "middle",
    )
)
s.append(
    t(
        490,
        412,
        "no covering lens? declined: “no governed lens covers this” — never a guess",
        11,
        INK,
        "middle",
    )
)
write("fig2-lens", s)

# ---- fig 3 · the loop, as steps -------------------------------------------
s = [svg_open(1060, 330)]
steps = [
    (60, "serve", ["governed answer", "+ receipt"], True),
    (310, "flag", ["the agent flags it —", "or the lens flags itself"], False),
    (560, "patch, as files", ["AI drafts, a human rules:", "definition · certified SQL"], False),
    (810, "gate", ["commit + apply —", "every past fix re-runs"], False),
]
for x, title, subs, hot in steps:
    s.append(
        box(x, 70, 190, 84, AMBER if hot else INK, AMBER_W if hot else SURF, 1.6 if hot else 1.1)
    )
    s.append(t(x + 95, 98, title, 13.5, AMBER if hot else INK, "middle", True))
    for i, sub in enumerate(subs):
        s.append(t(x + 95, 120 + i * 16, sub, 10, DIM, "middle"))
for x in (250, 500, 750):
    s.append(line(x + 2, 112, x + 58, 112, INK, "ai", 1.2))
s.append(
    f'<path d="M 905 154 C 905 268, 155 268, 155 154" fill="none" '
    f'stroke="{AMBER}" stroke-width="1.6" marker-end="url(#aa)"/>'
)
s.append(
    t(
        530,
        268,
        "served verbatim from then on — and fed back as example SQL, raising accuracy",
        11,
        AMBER,
        "middle",
        True,
    )
)
write("fig3-flywheel", s)

# ---- fig 3b · raise for review --------------------------------------------
s = [svg_open(1120, 340)]
s += [
    t(275, 104, "send for review —", 10, DIM, "middle"),
    t(275, 118, "the agent, or auto_review", 10, DIM, "middle"),
    t(545, 104, "the AI judge triages —", 10, DIM, "middle"),
    t(545, 118, "only confident approves resolve", 10, DIM, "middle"),
    t(840, 104, "the ruling lands as", 10, DIM, "middle"),
]
s += [
    box(40, 140, 210, 90),
    t(145, 172, "a served answer", 12.5, anchor="middle", bold=True),
    t(145, 194, "“48 400 € commission”", 10.5, DIM, "middle"),
    t(145, 210, "…did we include clawbacks?", 10.5, DIM, "middle"),
]
s.append(line(250, 185, 296, 185, INK, "ai"))
s += [
    box(300, 130, 200, 110),
    t(400, 160, "the ticket", 12.5, anchor="middle", bold=True),
    t(400, 182, "the full trace:", 10.5, DIM, "middle"),
    t(400, 198, "question · SQL · rows", 10.5, DIM, "middle"),
    t(400, 214, "answer · verification", 10.5, DIM, "middle"),
]
s.append(line(500, 185, 586, 185, INK, "ai"))
s += [
    box(590, 130, 210, 110, AMBER, AMBER_W, 1.6),
    t(695, 162, "a human rules", 13, AMBER, "middle", True),
    t(695, 186, "approve · changes · reject", 10.5, INK, "middle"),
    t(695, 204, "never auto-merged", 10.5, DIM, "middle"),
]
s.append(line(800, 185, 876, 185, INK, "ai"))
s += [
    box(880, 130, 200, 110),
    t(980, 160, "files in your repo", 12.5, anchor="middle", bold=True),
    t(980, 182, "the patch — definition", 10.5, DIM, "middle"),
    t(980, 198, "or certified SQL,", 10.5, DIM, "middle"),
    t(980, 214, "+ a regression eval case", 10.5, DIM, "middle"),
]
s.append(
    t(
        560,
        292,
        "a person doubted a number — the system turns that into a permanent guarantee",
        11,
        AMBER,
        "middle",
        True,
    )
)
write("fig3b-review", s)

# ---- fig 4 · how dst is authored ------------------------------------------
s = [svg_open(1180, 500)]
s += [
    box(60, 60, 220, 96),
    t(170, 90, "a definition changes", 12.5, anchor="middle", bold=True),
    t(170, 112, "definitions/revenue.md", 10, DIM, "middle"),
    t(170, 128, "now excludes returns", 10, DIM, "middle"),
]
s += [line(280, 108, 356, 108, INK, "ai"), t(318, 94, "commit · PR", 10, DIM, "middle")]
s += [
    box(360, 50, 290, 116),
    t(505, 78, "CI, on the pull request", 12.5, anchor="middle", bold=True),
    line(384, 90, 626, 90, LINE),
    t(384, 112, "dst plan — the diff · exit 1 invalid", 10.5, INK),
    t(384, 132, "dst test — exit 0 · 1 · 4", 10.5, INK),
    t(384, 152, "exit 4 = nothing was verified", 10, DIM),
]
s += [line(650, 108, 686, 108, INK, "ai"), t(668, 94, "merge", 10, DIM, "middle")]
s += [
    box(690, 40, 260, 136, AMBER, SURF, 1.6),
    t(714, 68, "dst apply — one transaction", 12.5, AMBER, bold=True),
    line(714, 80, 926, 80, LINE),
    t(714, 102, "✓ validate — static checks", 10.5, INK),
    t(714, 122, "✓ probe every connection", 10.5, INK),
    t(714, 142, "✓ eval suite — every certified", 10.5, INK),
    t(728, 158, "answer re-runs, executed", 10.5, INK),
]
s += [
    line(950, 80, 996, 68, AMBER, "aa", 1.5),
    box(1000, 36, 160, 74, AMBER),
    t(1080, 62, "published v8", 12.5, AMBER, "middle", True),
    t(1080, 81, "all gates green —", 10, DIM, "middle"),
    t(1080, 95, "atomic, serving now", 10, DIM, "middle"),
]
s += [
    line(950, 140, 996, 170, INK, "ai", 1.1),
    box(1000, 174, 160, 86),
    t(1080, 200, "rejected", 12.5, anchor="middle", bold=True),
    t(1080, 219, "any gate fails —", 10, DIM, "middle"),
    t(1080, 233, "nothing deployed,", 10, DIM, "middle"),
    t(1080, 247, "v7 keeps serving", 10, DIM, "middle"),
]
# the version rail: what is serving, the whole time
s += [
    line(60, 330, 1000, 330, INK, sw=2),
    line(1000, 330, 1160, 330, AMBER, sw=2.4),
    line(1000, 322, 1000, 338, AMBER, sw=2),
    t(
        60,
        316,
        "lens v7 — serving, untouched, through the edit, the PR, the CI run, and every gate",
        10.5,
        DIM,
    ),
    t(1004, 316, "atomic swap", 10, AMBER),
    t(1160, 352, "v8 serving", 10.5, AMBER, "end", True),
]
s.append(
    t(
        605,
        420,
        "rollback is git revert + dst apply · dst lens log lists every version · the CI runs the same verbs you do",
        11,
        DIM,
        "middle",
    )
)
write("fig4-authoring", s)

# ---- contact sheet ---------------------------------------------------------
FIGS = [
    (
        "fig1-shape",
        "Fig. 1 — the shape of the system",
        "A question arrives from the AI your team uses; dst decides who may ask, what the "
        "words mean, which tables carry it — and the answer returns cited and priced, leaving "
        "a receipt.",
    ),
    (
        "fig2-lens",
        "Fig. 2 — lenses, the foundational unit",
        "One lens per use case over the same warehouse, each with its own audience, data, and "
        "meanings. A question routes to the covering lens — or is declined, never guessed.",
    ),
    (
        "fig3-flywheel",
        "Fig. 3 — the loop",
        "A wrong answer ends as a file plus a regression test. Every past fix re-runs on every "
        "apply, and the corrected answer serves verbatim — and rides back into generation as "
        "example SQL.",
    ),
    (
        "fig3b-review",
        "Fig. 3b — raise for review",
        "Doubting a number is a first-class act: anyone — or the lens itself — sends the full "
        "trace for review, an AI judge triages, and a human rules. The ruling is files in your "
        "repo, never a setting someone toggled.",
    ),
    (
        "fig4-authoring",
        "Fig. 4 — publishing a refreshed lens",
        "The CI/CD experience: edit a definition, open a PR, CI runs the same dst verbs with "
        "real exit codes, and apply is one transaction through three gates. v7 serves untouched "
        "until the atomic swap; any failure deploys nothing.",
    ),
]

css = """
  :root { --paper:#faf6ee; --ink:#292524; --dim:#8a8178; --line:#ddd4c4; --amber:#b45309; }
  @media (prefers-color-scheme: dark) {
    :root { --paper:#171412; --ink:#e8e2d8; --dim:#8f867b; --line:#38312a; --amber:#e8930c; }
  }
  :root[data-theme="light"] { --paper:#faf6ee; --ink:#292524; --dim:#8a8178; --line:#ddd4c4; --amber:#b45309; }
  :root[data-theme="dark"]  { --paper:#171412; --ink:#e8e2d8; --dim:#8f867b; --line:#38312a; --amber:#e8930c; }
  html { background: var(--paper); }
  body { background: var(--paper); color: var(--ink);
    font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    line-height: 1.55; padding: 48px 24px 64px; }
  .sheet { max-width: 1000px; margin: 0 auto; display: flex; flex-direction: column; gap: 30px; }
  header .eyebrow { color: var(--amber); font-size: 11px; letter-spacing: .14em; text-transform: uppercase; margin-bottom: 10px; }
  header h1 { font-size: 23px; font-weight: 700; margin-bottom: 12px; }
  header .lede { max-width: 68ch; font-size: 13.5px; }
  figure { display: flex; flex-direction: column; }
  .panel { border: 1px solid var(--line); background: #faf6ee; padding: 10px; }
  .panel svg { display: block; width: 100%; height: auto; }
  figcaption { font-size: 12.5px; color: var(--dim); padding-top: 10px; max-width: 86ch; }
  figcaption b { color: var(--amber); font-weight: 700; }
"""

page = ["<title>dst — architecture figures</title>", f"<style>{css}</style>", '<div class="sheet">']
page.append("""  <header>
    <div class="eyebrow">data serve tool · architecture figures · v4 for review</div>
    <h1>Five figures, one claim each</h1>
    <p class="lede">v4: warehouse cost on the receipt as “your data”; lens cards with real
    people and 30-day stats; the review rail promoted to its own figure — a human rules,
    never auto-merged; fig 4 rebuilt around the CI/CD story — PR, real exit codes, three
    gates, an atomic version swap while v7 serves untouched.</p>
  </header>""")
for name, title, cap in FIGS:
    svg = (OUT / f"{name}.svg").read_text()
    page.append(
        f'  <figure>\n    <div class="panel">{svg}</div>\n'
        f"    <figcaption><b>{html.escape(title)}</b> — {html.escape(cap)}</figcaption>\n  </figure>"
    )
page.append("</div>")
(BASE / "dst-architecture-figures.html").write_text("\n".join(page))
print("wrote", len(FIGS), "figures +", BASE / "dst-architecture-figures.html")
