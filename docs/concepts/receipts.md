# Receipts

Every answer ships with its receipt. The response carries the answer, the
rows, **the SQL that produced them**, citations, a confidence grade with the
per-check verification report behind it, certification and who vouched for it
and when, `data_as_of` (how fresh the data was), and a `request_id`. Where an
answer came from is not a log you dig up afterwards. It is part of the output.

## What one receipt contains

- **Who asked.** The caller identity, resolved from the API key and written
  into the trace. A caller is the **person behind the agent**: an agent acts
  for a person, and is never a caller on its own account. Doctrine: one key
  per person, never one per tool. However many agents someone runs, they all
  ask under that person's key. Every answer traces back to somebody who can be
  asked about it. An unattended app is no exception: its key belongs to the
  person accountable for it.
- **Which lens** answered, and what the allow-list decided. Both allow *and*
  deny outcomes are written to `audit_log`.
- **What it cost**, on both meters: AI tokens and their USD cost, and
  warehouse bytes and their cost, plus latency — all on the per-request trace.
  One rule with teeth: if any model used has no known price, the whole trace's
  cost is `NULL`, never a default. A partial sum shown as the total would be a
  quiet lie.
- **How much to trust it.** The verification grade
  (`verified | partial | unverified`) rests on deterministic checks — the same
  input always grades the same: every number in the answer traceable to the
  rows, the definition applied, the SQL matching the question's intent, the
  result not cut short, the data fresh enough. A lens that opts into the LLM
  judge or the adversarial reviewer folds their verdicts onto the same report,
  and those can demote the grade — visibly, as their own named checks.
- **The SQL.** Always attached to a served answer. A *rejected* response never
  includes the SQL it refused to run; the trace keeps it for review.

`data_as_of` and certification details appear only in the response. The model
never sees them, so it cannot fake them.

## The signed block

A number pasted into a slide, a ticket, or another agent's context used to
carry nothing a skeptic could check. So every data answer ships a small
portable receipt block (`request_id`, `lens`, `served_at`, `certification`,
`cert_id`, `confidence`, `sql_sha256`, `data_as_of`, and a `digest`). Anyone
in the org can send it back (`POST /v1/verify-receipt`, or the
`verify_receipt` MCP tool) and learn whether these exact claims were really
served, by this server. The signature is recomputed, and every field is
checked against the logged trace.

- The signature is an **HMAC-SHA256 over canonical JSON** (keys sorted, the
  digest field excluded) — a standard keyed checksum only someone holding the
  key can produce. The key is `DST_SECRET_KEY`, with the same key list and
  rotation contract as stored-secret encryption: the first key signs, every
  key verifies.
- The block carries the SQL's **hash**, not the SQL itself. Receipts travel
  further than SQL should, and the hash still pins the receipt to the exact
  query.
- Verification is **stateless**: nothing new is saved. It recomputes the
  signature and reads the `request_log` row that serving already wrote.
- No key configured? The receipt ships with `digest: null`, and verification
  reports **unsigned** out loud. Faking a digest, or refusing to serve for
  lack of one, would both be worse. A receipt whose digest this server has no
  key to check reports **unkeyed**: a configuration gap named as itself, never
  as forgery.
- **Refusals and clarifications carry no receipt**: they make no data claim to
  attest.

## The freshness contract

Two facts ride the answer: one measured, one declared. `data_as_of` is
**measured** — read from the stored table profiles, never asserted.
`stale_after_days` is what you **declare** on the lens: how old is too old for
this use case. Nobody downstream can infer that; only you know it. It sits on
the lens rather than on a table because tolerance belongs to the question, not
the data. The same orders table is fresh enough for a monthly close and too
stale for an ops board, and only the use case knows which.

When the data is older than the contract allows, the freshness check fails,
the grade is demoted, and the answer says so. Even a certified serve past the
contract is still stale. If you never declare `stale_after_days`, the check
reports `skip` — never a pass it didn't earn.

Know how `data_as_of` is measured before you set the contract. It is the
**oldest last-update across every entity in the lens's scope** — not just the
tables the answer actually touched — and it reads profiles from the lens's
first declared connection only. Two consequences. A single rarely-updated
table in scope (a country lookup that legitimately hasn't changed in months)
drags `data_as_of` down and can mark every answer from that lens stale, even
answers that never read that table. And on a lens with two connections,
freshness on the second connection is not measured at all. So set
`stale_after_days` against the slowest thing in scope, not against the table
you have in mind — or split the slow-moving table into its own lens. Leaving
it unset is a reasonable default until you have checked what `data_as_of`
actually reports for that lens.

## The receipt is the platform engineer's audit log

Traces persist to `request_log`, scoped per organization and enforced by
row-level security in the database. The Observe views are summaries read from
that same table. There is no separate, prettier record that could drift from
the evidence. Per-caller cost roll-ups, wrong-answer investigations, and
access reviews all read from the same rows the requests wrote.

The receipt is a first-class output rather than an operational side effect:
caller, lens, cost, grade, and SQL ride with the answer itself, so an answer
can always be traced back to who asked and what produced it.
