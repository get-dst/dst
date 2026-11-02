# Question types

Not every question asked of a data tool asks for a number. In a day's traffic a
fair share ask what a term means, whether the data holds something, for the SQL
behind a figure, which report to open, what the caller asked earlier, or
nothing about data at all. Read as data questions, each of those fails in the
same way: the word check stops on a function word ("means", "want", "where"), or
the router finds a measure nobody wanted a figure for and the answer is a
clarification about the wrong thing.

So every door runs one step before any binding: what kind of thing was asked.
Seven types come out of it, and only one of them, `query`, goes on to the
ledger, the resolver and the warehouse.

| Type | Asked like | What comes back | Mode |
|---|---|---|---|
| `query` | "orders by status last month" | the governed answer, as on every page of these docs | the data question's own, if any |
| `definition` | "what does SQO mean", "how is win rate calculated", "difference between gross and net revenue" | the member's description, expression and filters, its aliases, its dictionary values, the definitions page; two members side by side for a difference. Nothing runs. | `definition` |
| `capability` | "do we have NPS", "is it possible to split revenue by region" | yes, naming the member that holds it and offering the question to ask; or no, naming what is declared; a `not_computable` entry is quoted as written | `capability` |
| `sql` | "give me the SQL for revenue by month" | the data question is served as usual and the compiled SQL (and the Cube query, on a Cube lens) is the answer body; the rows come with it. "Give me the SQL" alone returns the SQL of the caller's last answer | `sql_request` |
| `locate` | "what report shows churn", "where can I find the margin dashboard" | the project's `hand_off:` entries. dst keeps no inventory of reports. "Which lens holds X" names the lens and member, then offers the question | `hand_off` |
| `admin` | "show me my last request", "my last 3 queries" | the caller's own rows from the request log: when, what, which lens, what came back. Never another caller's | `admin` |
| `not_a_data_question` | "email this to Mika", "how do I export this", "hello", "what is the meaning of life" | a refusal saying so, naming the hand-off | `not_a_data_question` |

## How the type is decided

The rules are deterministic and run first, in order of how sure each is: the
caller's own log, a SQL ask, a difference, a definition, a capability, a
place, an errand or a greeting. Each rule is a table of phrasings per
language; dst ships the English table ("what does X mean", "difference
between", "do we have", "where can I find"), tried in order; the receipt's rule names
which decided. A question with a data shape (a count, a
ranking, a period, a comparison, a number) is a query, and so is one holding a
word some lens the caller may use declares. A question holding neither is left
open, never declared off-topic on the catalogue's silence: the word may be a
column of an exposed table.

What the rules cannot decide is left open. On the door with no driver agent (a
person asking over HTTP, or an app) a model reads the open case once, from a
fixed list of the seven types, and an answer outside the list is a query. In
driver mode (every MCP caller, or a caller an admin marked a driver) no model is
asked: the open case is a query, as before. With no model configured, the same.

The subject of a definition or a capability ask is matched against the members
the caller may see: by name, title or alias first, then by description or a
stored value. A member hidden from the caller's groups is not there to be
defined. Nothing is invented: a term no lens defines is said to be undefined,
and the answer names what the lens does declare.

## Where the answers come from

- A lens served through Cube: the lens model stored at apply (its members,
  `meta.dst` aliases and caveats, the overlay's definitions and ambiguous terms,
  `not_computable`), with the probe's dictionaries for the values.
- A lens on the typed core: its entities' metrics (description, aggregation or
  expression, filters, aliases), dimensions, and the definitions pages.
- `hand_off:` in `dst.yaml`, a list of places a question that is not dst's
  goes to ([Configuration](../reference/configuration.md#hand-off-hand_off)).
- The request log, scoped to the organisation and to the caller by name.

The routed door reads every lens the caller may use and writes the row under
the lens that held the subject. The lens door reads that lens alone.

## Counted apart

Each type carries its own mode, so `dst observe` prints them on a `question
types:` line beside `modes:`, and `dst test` tallies them as `by_type` beside
`by_mode`. A term explained is not a refusal, and the refusal counts stay about
data questions. A behavioural case can pin the type:
`expect: definition | capability | sql_request | hand_off | admin |
not_a_data_question` passes when the response carries that mode, whatever its
status ([Evaluation](../guides/evaluation.md#behavioral-cases-pinning-shape)).

## The ledger's part

Two changes ride with the step. The word ledger has a stop-list of glossing
words ("means", "want", "can", "where", "through", "please") that name nothing in
any model, so a polite sentence no longer stops the ledger. And operator words
("average", "rate", "share", "conversion", "growth", "drop") are read as an
operation over the measures the question names, never as a thing of their own:
the check passes when the query carries the operation (an average measure; a
ratio measure or two measures composed; a comparison window or a growth measure)
and otherwise asks, naming the measures of the lens that carry it. A share
served as the plain total, or an average served as a sum, does not pass.
