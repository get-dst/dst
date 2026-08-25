# FAQ

Short answers; each links to the page that carries the mechanism.

## Why not just give my agent the schema and let it query?

Because then the agent holds warehouse credentials, writes its own SQL, and
nothing checks the result. With dst the agent never touches the warehouse: it
asks a question, and dst grounds it in your definitions, generates and guards
the SQL, executes it read-only, and returns a cited answer with the SQL and a
verification grade attached. How that works, step by step:
[the answer path](concepts/answer-path.md).

## Why did it refuse my question instead of answering?

Because a wrong answer is worse than no answer. A question that uses an
ambiguous term without picking a meaning gets a clarification — dst asks which
meaning you want. A question naming a metric the lens deliberately excluded is
refused before any model call. And when the data to answer doesn't exist, dst
declines, never serves a confident zero. All of it is enforced in code that
runs before or after the model, never by a prompt rule alone, because a prompt
rule is a request and models do violate them.
See [Clarification & refusal](concepts/clarify-and-refusal.md).

## Why does this answer have no prose, just a data frame?

The figure gate fired. Every number in a written answer must be traceable to
the rows that came back. When that check fails, the sentence is rewritten once;
when the retry fails too, the prose is withheld entirely: the response sets
`composition: "fallback"` and the answer becomes a table and summary written by
code, never model prose. The data is true; the sentence was not trustworthy,
so you didn't get one. A degraded-but-true answer is an outcome; an invented
figure is not. See [the answer path](concepts/answer-path.md).

## Which LLM does it use? Do I bring my own keys?

Your models, your keys. Providers are declared in `dst.yaml` by the API shape
they speak, not by vendor name: `anthropic`, `openai-compatible` (covers
OpenAI, DeepSeek, Ollama, vLLM, most gateways), and
`local` for keyless in-process embeddings. The
core knows API shapes; your config knows vendors. Keys enter as env-var names
only; a key pasted into a committed file is a parse error. See
[Configuration](reference/configuration.md).

## What is open source?

The whole governed serving stack, under Apache-2.0: lenses, the query pipeline and its
guards, certified answers, the review queue, the router, MCP, the dashboard.
Bootstrapping a layer from query history is in here too — it ships as the scaffolded
[history-bootstrap skill](guides/drift-audit.md) your own agent runs, not as a server
feature. No behavior is gated behind an edition flag: `DST_EDITION` is a UI badge only,
and the core never reads it to decide anything.

## Why can't I create a lens in the dashboard?

Files author; the UI governs. Lenses, entities, definitions, and certified answers
live in your repo and deploy through `dst plan` / `dst apply` — versioned,
diffable, reviewable like the rest of your code. The dashboard is for ruling on
reviews, watching cost and drift, and browsing versions. If the UI could edit
state on the server, the next apply of your unchanged files would silently undo
the edit. See
[Project files](guides/project-files.md).

## Which warehouses does it support?

Five warehouse connectors (DuckDB, Postgres, MySQL, BigQuery, Snowflake), each
read-only in layers: the SQL is guarded and SELECT-only first, and a read-only
credential or session is the backstop. See
[Connect a warehouse](guides/connect-a-warehouse.md).

## Do I have to write an eval suite?

No: the certified library is the suite. Every active certified answer doubles
as a regression test. Its stored SQL runs as the known-good reference, the
question is asked again through generation with certified matching switched
off, and the two executed results are compared. `dst test` runs the full
corpus; the same check gates `dst apply`, scoped to the answers a push
actually touches. See
[Evaluation](guides/evaluation.md) and [Certified answers](concepts/certified.md).

*(Every answer here is covered in full on a linked page.)*
