# The signature — what makes a dst screen unmistakably dst

The identity spec the dashboard's core screens implement. Every element here
exists to counter a specific way generated frontends look interchangeable; a
flourish that counters nothing is decoration and doesn't ship. Genuine is not
the same as loud.

The base identity stays: amber + warm paper + mono, token names stable
(`apps/web/src/index.css`). This spec pushes it from "clean default" to
"owned" with three recurring elements and a set of microtypography rules.

## 1. The fascia

Every page opens with an instrument fascia, not a floating heading: the title
block seated on a full-width hairline baseline (`border-border-strong`), with a
**right-aligned mono readout of live facts** — counts, cost, last event — on
the same baseline. Title left, instruments right: the page is asymmetric from
the first line, and the first line already carries data.

Implementation: `PageHeader` (`components/ui/Page.tsx`) owns the fascia; pages
pass `readout`. No page hand-rolls its own heading.

## 2. The readout grammar (the provenance signature)

One way to render machine facts, everywhere: `label value` pairs in 11px mono,
tabular figures, `·` separators; labels muted, values ink. Status is a mono
glyph column (`✓ ✗ ·`), never an icon. This is the grammar of receipts — the
product's pitch is answers-with-receipts, and the chrome renders like one.

Implementation: `components/ui/Readout.tsx`. Used in the fascia, table
footers, and anywhere provenance renders (caller · lens · cost · time).

## 3. The ledger

Dense collections are ledgers, not trays of cards: one container, hairline
row dividers, shared column baselines, square edges against the page (the
container's rule runs full-bleed to the content column, `border-y`, no
rounded-card costume). **Weight follows evidence**: a lens with 400 queries
gets its numbers and description at full height; an empty one compresses to a
single dense line. Unequal treatment is the information.

Implementation: the Lenses index is the reference ledger. New collection
surfaces copy it rather than reaching for a card grid.

## Microtypography

- Mono (`--font-mono`) for every identifier, number, path, and timestamp —
  no exceptions; tabular figures always (set globally in `index.css`).
- Section labels are `panel-label`: 10px mono uppercase, 0.12em tracking,
  muted — the label style Router already used; now a utility, not an idiom
  to rediscover.
- Prose measures are bounded (`max-w-[64ch]` on descriptions) — full-width
  gray paragraphs are template filler.
- Inter is the body face. Mono-for-data density plus the fascia is what
  carries the signature; the body face is not doing that work.

## The test

Strip the wordmark from a screenshot. If the fascia readout, the mono density,
and the ledger weighting don't identify the app in one glance, the change that
removed them fails review. `scripts/genuine_lint.py` catches the mechanical
half of this; the rest is a human read.
