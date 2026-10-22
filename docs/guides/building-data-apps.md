# Building data apps

A data app on dst (data serve tool), such as a metric tree, a KPI board or a drill-down
page, reads its figures through the same doors an agent uses. This page describes the
pattern: which call each part of the screen makes, and why that call and not another.
The calls are plain HTTP with a caller key; [Build your own UI](build-your-own-ui.md)
covers credentials, CORS and typed clients.

## A node is a typed intent

Each figure on the screen is one structured intent sent to the metrics door,
`POST /v1/lenses/{lens}/metrics`. The intent names a metric, the dimensions to group
by, the filters, and optionally a `grain`. The names come from the lens's catalog
(below). An unknown one is a 400 that says which.

```bash
curl -s http://localhost:8000/v1/lenses/memberships/metrics \
  --header "Authorization: Bearer dst_..." \
  --header "Content-Type: application/json" \
  --data '{"metrics": ["net_new_fees"],
           "filters": [{"field": "month", "op": ">=", "value": "2026-09-01"},
                       {"field": "month", "op": "<", "value": "2026-10-01"}]}'
```

The metrics door is the right one for a node for three reasons. It runs the same
compiler that typed AI answers go through, so a number in the app and the number an
agent quotes for the same metric and window are built by the same code. It is
deterministic: the same intent over the same data compiles to the same SQL. It makes no
model call, because the door defaults to `format: "structured"`, which returns rows
without composing prose. What comes back is the full answer envelope, the same one a
question gets: the `sql` that ran, the resolution ledger, the graded `confidence`,
`data_as_of`, a `request_id` and a signed `receipt`
([Receipts](../concepts/receipts.md)).

Keep each node's `request_id`. It is the handle a user's "this number looks wrong"
needs: `POST /v1/reviews` with it opens a ticket in the
[review queue](correction-loop.md).

## The tree's structure lives in dst

How the nodes relate is part of the semantic layer, not the app. A parent computed from
its children is a derived metric whose `expr` names them as `{placeholders}`, and a
ratio names its `numerator:` and `denominator:`
([Project files](project-files.md#what-the-metric-catalog-shows)).

The app reads that structure from the catalog:

```bash
curl -s http://localhost:8000/v1/lenses/memberships/catalog \
  --header "Authorization: Bearer dst_..."
```

Every metric in it carries `depends_on`, the metrics it is computed from, and `tree`
arranges them: the roots are the metrics nothing else depends on, and each node's
`children` are its `depends_on`. The same entries carry what the app needs to draw a
node: `label` and `aliases` for the title, `unit` for the number format, `additivity`
for whether the figure is a total or a snapshot, `grains` for the trend picker, and
`dimensions` for the drill-down menu. The catalog's `dimensions` list gives each
dimension's complete `values` where the stored profile holds a complete dictionary,
which is what a filter dropdown needs. Elsewhere `values` is null. A sampled list is
never served as the whole one.

Because the formula is declared once, the app never holds a second copy of it. When the
lens author changes `net_new_fees`, the next `dst apply` changes the catalog, the tree
and every number under it together.

## Certified answers for signed nodes

Some nodes should not be compiled at all. A figure someone signed off for the board, or
one the compiler cannot express (a median, a cohort calculation), belongs in a
[certified answer](../concepts/certified.md): a question and its SQL, approved by a
named person.

An app runs a certified answer by its id, never by its question text:

```bash
curl -s http://localhost:8000/v1/lenses/memberships/certified/<cert_id>/run \
  --header "Authorization: Bearer dst_..." \
  --header "Content-Type: application/json" \
  --data '{"bindings": {"period": "2026-Q3"}, "format": "structured"}'
```

Matching by question text is a similarity search. A node that must show the signed
figure names it, rather than hoping a paraphrase scores high enough. The catalog
links the two: each metric's `certified_answer_ids` lists the active certified answers
whose SQL reads it, and `GET /v1/lenses/{lens}/certified` lists every answer with its
`slots` and `sample_bindings`. A template answer takes its slot values in `bindings`,
which are validated and rendered as typed literals; a missing or invalid binding is a
400 naming the slot.

What a certified run adds over a compiled node: `certification: "certified"` with who
certified the pair and when, and the bound values for a template. The `restated` check
compares the live result with the value recorded when the answer was verified. A moved
result caps the grade at `partial`, with a `RESTATED` line on `degraded` naming the old
value and the new one. The definitions that govern the SQL are disclosed on the
answer's basis line, in `definition_used` and on the resolution ledger, so a signed
number always says which meaning it is.

## The "why" follow-up is a question

A node answers "what is the figure". When the user then asks why upgrades fell, that
is a question, and it goes to a natural-language door:
`POST /v1/lenses/{lens}/query`, or `POST /v1/query` to let dst choose the lens. Send
`purpose` with it, one sentence on what the app is for ("membership fee bridge for the
monthly business review"). It is stored on the request's log row when the lens sets
`logging.log_purpose: true`, so the lens owner can see what apps ask, and it never
changes what a term means. The answer may come back as a `clarification` instead of a
figure; that is a governed non-answer, and the app should show its options and re-ask
([Clarify and refusal](../concepts/clarify-and-refusal.md)).

## Render the tree from one batch

A tree drawn from separate requests can show a parent and its children from two
different moments. `POST /v1/lenses/{lens}/metrics/batch` takes up to 50 intents, each
with an `id`, and runs them in one request:

- The profile is read once, so every answer carries the same `data_as_of`.
- On Postgres every query reads one exported snapshot, and the response says
  `"snapshot": "shared"`. On a connector that cannot hold one snapshot across
  connections, each query reads the warehouse at its own moment and the response says
  `"per_query"`. The app can show that fact rather than assume it.
- Results come back under `results`, keyed by the intent ids, each with its own
  `request_id` and receipt.
- A failing intent fails alone: its entry has `ok: false`, the HTTP `status` the single
  door would have answered and the `error`. That includes an intent the door answered
  without serving it, whose `answer.status` is `error` or `rejected`: its `status` is 200,
  `error` is the answer's sentence, and the `answer` is there too. A clarification or a
  refusal is an answer, so it comes back `ok: true` with its own `answer.status`. The app
  draws a node with `ok: false` as an error and the rest as usual.

Each intent counts against the caller's rate limit and daily quota as a single ask
would. Intents past the limit are answered 429 individually, so a large tree on a tight
quota renders partially rather than not at all.

## Every node shows its delta

Add `compare` to an intent and each metric comes back beside a second window:

```json
"compare": {"to": "previous_period"}
```

`to` is `previous_period` (the window of equal length just before; whole calendar
months when the window is month-aligned), `previous_year`, or an explicit range such as
`2025-09` or `2025-09-01/2025-09-30`. Every metric gains three columns:
`<metric>_compare`, `<metric>_delta` and `<metric>_delta_pct`, the last being the delta
over the absolute compare value.

The comparison window is derived from the intent's own date filters, so an intent
needs a window bounded on both ends. Without one the compile is refused with a
sentence naming the field to bound; dst does not pick a "this month" on the app's
behalf. Each window is compiled on its own. That keeps the metric's reading: a `level`
metric such as monthly fees is read at each window's latest snapshot, and a flow such as new
fees is summed over each window. Under a `grain`, each bucket is matched with its counterpart
in the other window, and a grain that cannot line up with the shift is refused rather
than matched approximately. The second window is recorded on the resolution ledger, so
the compare column is never mistaken for a figure of the asked window.

## Worked example: a membership fee bridge

Monthly membership fees are a snapshot table with one row per member per month end. The
bridge from one month's fees to the next is a table of movements: one row per member per
month per kind of change.

```yaml
# semantic/entities/fee_snapshots.yaml
name: fee_snapshots
source: { connection: warehouse, table: finance.fee_snapshots }
default_time_field: month
fields:
  - { name: month, type: date }
  - { name: member_id, type: string }
  - { name: location, type: string }
  - { name: fee_eur, type: number }
metrics:
  - name: monthly_fees
    label: monthly membership fees
    aliases: [membership fees]
    description: Monthly membership fees on the books at the month-end snapshot.
    agg: sum
    expr: fee_snapshots.fee_eur
    additivity: level
    currency: EUR
```

```yaml
# semantic/entities/membership_movements.yaml
name: membership_movements
source: { connection: warehouse, table: finance.membership_movements }
default_time_field: month
fields:
  - { name: month, type: date }
  - { name: member_id, type: string }
  - { name: movement, type: string }
  - { name: amount_eur, type: number }
metrics:
  - name: new_fees
    label: new fees
    agg: sum
    expr: membership_movements.amount_eur
    filters: ["membership_movements.movement = 'new'"]
    currency: EUR
  - name: upgrade_fees
    label: upgrade fees
    agg: sum
    expr: membership_movements.amount_eur
    filters: ["membership_movements.movement = 'upgrade'"]
    currency: EUR
  - name: downgrade_fees
    label: downgrade fees
    agg: sum
    expr: membership_movements.amount_eur
    filters: ["membership_movements.movement = 'downgrade'"]
    currency: EUR
  - name: cancelled_fees
    label: cancelled fees
    agg: sum
    expr: membership_movements.amount_eur
    filters: ["membership_movements.movement = 'cancel'"]
    currency: EUR
  - name: net_new_fees
    label: net new fees
    type: derived
    expr: "COALESCE({new_fees}, 0) + COALESCE({upgrade_fees}, 0)
      - COALESCE({downgrade_fees}, 0) - COALESCE({cancelled_fees}, 0)"
    currency: EUR
```

The `COALESCE` matters: a month with no downgrade rows sums to null, and null
arithmetic would make the whole bridge null for that month.

With both entities selected by a lens named `memberships`, the catalog's `tree` reads
(abbreviated):

```json
[
  {"metric": "monthly_fees", "entity": "fee_snapshots",
   "label": "monthly membership fees", "children": []},
  {"metric": "net_new_fees", "entity": "membership_movements", "label": "net new fees",
   "children": [
     {"metric": "new_fees", "label": "new fees", "children": []},
     {"metric": "upgrade_fees", "label": "upgrade fees", "children": []},
     {"metric": "downgrade_fees", "label": "downgrade fees", "children": []},
     {"metric": "cancelled_fees", "label": "cancelled fees", "children": []}
   ]}
]
```

and its entry for `monthly_fees` carries `"unit": "EUR"`, `"additivity": "level"`,
`"time_field": "month"`, `"grains": ["day", "week", "month", "quarter", "year"]` and
`"dimensions": ["month", "member_id", "location"]`.

The app walks the tree and sends one intent per node, plus a per-member table for
monthly fees, all in one batch and all compared with the previous month:

```bash
curl -s http://localhost:8000/v1/lenses/memberships/metrics/batch \
  --header "Authorization: Bearer dst_..." \
  --header "Content-Type: application/json" \
  --data @- <<'JSON'
{"intents": [
  {"id": "monthly_fees", "metrics": ["monthly_fees"], "filters": [
     {"field": "month", "op": ">=", "value": "2026-09-01"},
     {"field": "month", "op": "<", "value": "2026-10-01"}],
   "compare": {"to": "previous_period"}},
  {"id": "fees_by_member", "metrics": ["monthly_fees"], "dimensions": ["member_id"],
   "filters": [
     {"field": "month", "op": ">=", "value": "2026-09-01"},
     {"field": "month", "op": "<", "value": "2026-10-01"}],
   "compare": {"to": "previous_period"},
   "order_by": [{"field": "monthly_fees", "dir": "desc"}], "limit": 20},
  {"id": "net_new_fees", "metrics": ["net_new_fees"], "filters": [
     {"field": "month", "op": ">=", "value": "2026-09-01"},
     {"field": "month", "op": "<", "value": "2026-10-01"}],
   "compare": {"to": "previous_period"}},
  {"id": "new_fees", "metrics": ["new_fees"], "filters": [
     {"field": "month", "op": ">=", "value": "2026-09-01"},
     {"field": "month", "op": "<", "value": "2026-10-01"}],
   "compare": {"to": "previous_period"}}
]}
JSON
```

The remaining bridge nodes follow the same shape. The response holds `data_as_of` and
`snapshot` once, and one entry per id. The `monthly_fees` entry's `data.columns` are
`monthly_fees`, `monthly_fees_compare`, `monthly_fees_delta` and `monthly_fees_delta_pct`:
September's month-end fees, August's, and the change between them. The `fees_by_member`
entry has the same four columns after `member_id`, ordered by September fees; a member
present only in August appears with a null `monthly_fees` and its August figure in
`monthly_fees_compare`.
Each entry has its own `request_id`, which the app keeps beside the node it drew.
