# data serve tool (dst)

dst sits between your AI and your data warehouse. The AI asks questions in
plain language; dst answers them from your data, shows its work, and keeps a
record of every call.

## Why AI gets your numbers wrong

Point an AI at a warehouse and it will answer everything, confidently. It
guesses which table means "revenue", makes up the definition on the spot, and
returns a number that looks exactly like a right one. Ask twice and you get two
different answers. Nobody can say where either came from, and nothing stops the
same mistake from happening again tomorrow.

The problem is not the model. The problem is that nothing between the question
and the warehouse holds a definition still, checks the answer against it, or
remembers the last correction.

## What dst is

[![One entry point — the AI your team uses calls dst over one governed interface; dst decides who may ask, what the words mean, and what SQL runs, and every call lands in the audit ledger with its cost](assets/figures/fig1-shape.svg)](assets/figures/fig1-shape.svg)

Your team already asks AI about your data — in Claude, in ChatGPT, in Copilot,
or through agents you run yourselves. dst is the layer those AIs call instead
of connecting to the warehouse directly. When a question comes in, dst:

1. checks **who is asking**, and what they are allowed to see;
2. looks up **what the words mean for your company** — your definitions, not
   a guess;
3. writes the SQL, checks it, and runs it against your warehouse —
   **read-only, always**;
4. returns the answer together with the SQL that produced it, a confidence
   grade, and what the call cost.

Every call is recorded: the question, the SQL, the answer, the cost, and who
asked.

There is no search box and no query screen. People ask through the AI they
already use; dst's job is making sure what comes back is right. The dashboard
is for running the system — reviewing, granting access, watching usage — not
for asking questions.

dst is not your data analyst. It is your data *janitor*: it knows which table,
which definition, which join, and serves exactly that, correctly, every time.
The AI does the thinking; dst keeps the numbers straight.

For the engineer, the whole product is a folder of files: edit a definition,
run `dst plan` to see what would change, `dst apply` to publish it.

[![dst on screen: the project files, a definition edit highlighted, and dst plan, apply, and test passing in the terminal](assets/product-on-screen.png)](on-screen.md)

*The files are the product. [See it with clickable tabs →](on-screen.md)*

## How it gets more accurate over time

AI is nondeterministic by nature. What you write into a prompt does not make a
model behave, and a system does not *improve* on its own. dst **puts a collar
on it**, built from the controls
data teams already trust — versioned deployments, regression tests, audit
trails, evals — run as a loop: catch a mistake, fix it, and make sure it
cannot come back. dst can run that loop because it sits on the whole path
between the question and the warehouse:

[![The loop: serve, flag, patch as files, gate — and the corrected answer is served verbatim from then on, fed back into generation as example SQL](assets/figures/fig3-flywheel.svg)](assets/figures/fig3-flywheel.svg)

- **Changes are deployments, not edits.** Your definitions live in files, in
  version control. `dst plan` shows what a change would do before `dst apply`
  publishes it, and a broken or self-contradictory change is rejected before
  anyone can be served an answer from it.
- **A mistake fixed once stays fixed.** When someone reports a wrong answer,
  the [correction loop](guides/correction-loop.md) turns the fix into a file
  plus a test. The test re-runs on every publish, so the same mistake cannot
  quietly come back — and you can prove it.
- **Every answer shows its work.** The SQL that ran, a confidence grade, and a
  [receipt](concepts/receipts.md) anyone can check later. Every access granted
  and every access denied is logged.
- **You can watch it get better.** The observe screen counts answered,
  declined, and failed separately — a polite "I can't answer that" is an
  outcome, not an error — and tracks what every call costs. As fixes and
  [certified answers](concepts/certified.md) accumulate, the numbers improve,
  and you can point at why.

Start with [the answer path](concepts/answer-path.md), then the
[quickstart](quickstart.md). Rather have it run for you? [dst Cloud](cloud.md).
