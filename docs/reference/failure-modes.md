# Failure modes

When dst does not return a clean answer, the response says why in one word. A
refusal, a clarification and an answer whose confidence was capped each carry
exactly one `mode`, chosen from the twenty names below. The same name appears on
the response, the receipt, the request log, `dst test` and `dst observe`, so a
count by mode reads as a list of work: each name points at the file or the
component that fixes it.

A mode is a governed outcome, not a fault. When dst itself breaks (it wrote SQL
that does not parse, the warehouse timed out), the response is `status: error`
and carries no mode.

## Where it appears

| Surface | Field |
|---|---|
| Query response | `mode`, next to `status`; a clarification also carries it as `clarification.mode` |
| Routed door, nothing covered | `mode` on the uncovered envelope (`unroutable` or `provider_down`) |
| Receipt | `mode` when a check capped the answer (`data_gap`, `stale`, `unverifiable`); signed with the rest |
| Request log | the `mode` column, one value per row; `dst observe` prints the counts as `modes:` |
| `dst test` | `mode` on every behavioural row; the summary prints the counts as `modes:`, and `--json` carries `by_mode` |
| `dst plan` / `dst apply` | each validation finding carries the `mode` it prevents, where it prevents one |
| `dst drift` | findings that change what a reader gets back carry `stale`; informational kinds carry none |

`clarification.kind` (`ambiguous_term`, `unknown_value`, `unresolved_slot`,
`named_table`) is still populated for one release and is deprecated. Read `mode`.

## The twenty modes

Grouped by the stage that detects each one, the earliest deterministic point it can be
known (`services/contracts/failure_modes.py`).

### Routing

| Mode | Meaning | Usually fixed in |
|---|---|---|
| `unroutable` | No lens the caller may use covers the question's words, or the covering lenses tie. | `lens.yaml` anchors, `use_when`, `sources` |
| `misrouted` | The lens chosen does not hold what was asked; it declined and named where to ask instead. | `lens.yaml` `not_for`, `sources`, `scope` |

### Resolve

| Mode | Meaning | Usually fixed in |
|---|---|---|
| `unasked_filter` | A filter value the question never named would have been applied. | dst (the resolver) |
| `unbound_value` | A value the question names is not a stored value of any column the lens declares. | `value_aliases`, the probe dictionaries |
| `ambiguous_metric` | More than one metric, clock or meaning carries the asked word and the question does not settle it. | `semantic/definitions/` (`status: ambiguous`, `audiences`), `not_for` |
| `not_declared` | The asked entity, metric, dimension or split does not exist in the layer, or the layer declares it not computable. | `semantic/entities/`, `not_computable` |
| `not_expressible` | Every piece is declared, but the answer's shape cannot be built in one query (a dimension on another entity, a share, a period through a join). | dst; until then a certified answer |
| `period_unreadable` | A period phrase the window parser does not read. dst asks for the window instead of guessing one. | dst (the window parser), or a `bindings` window from the caller |

### Verify

| Mode | Meaning | Usually fixed in |
|---|---|---|
| `uncovered_word` | A word of the question maps to nothing in the answer: not the metric, a filter, a dimension, the period or a definition. | metric `aliases`, `semantic/definitions/` |
| `window_misbound` | The window was read correctly and applied to the wrong clock or the wrong column type. | dst; a text-typed clock on an entity |
| `data_gap` | The window reaches past where the data runs; the gap is stated and confidence is capped. | `probe.max_age`, blueprint `clocks` |
| `empty_result` | Zero rows or a NULL figure where rows should exist; served as a finding, never as `0`. | the filter value, or the dictionary |
| `wrong_grain` | The grouping, ranking or population is not the asked subject. | entity `additivity`, blueprint `grain` |
| `partial_ask` | A question with several parts would be answered for fewer than all of them. | dst; certified answers covering the parts |
| `scope_violation` | The SQL reads a table outside the lens or the caller's scope. | `expose:`, or ask the lens that holds it |
| `hidden_column` | The SQL reads a column the lens hides from this caller. | `hide` / `except_groups` |
| `external_access` | The SQL reaches outside the warehouse connection (a file, a URL, another database). | not configurable: refused on every door |
| `unverifiable` | A check could not run (no blueprint key, a table over the sampling budget, no embedder); served only with confidence `unverified`. | blueprints, `dst probe`, the embedder |

### Serve

| Mode | Meaning | Usually fixed in |
|---|---|---|
| `stale` | The data or the layer is older than declared, or a table a reader depends on changed since the baseline. | `dst probe`, `dst drift --accept` |
| `provider_down` | A provider the answer needs (typed decider, router decider, embedder, chat model, warehouse, Cube) is unavailable; the request names it. | provider configuration |

## Apply and providers

`dst apply` demands only the providers a lens's lanes use. A lens with
`untyped_fallback: false` and no `model:` pin applies on an install whose only
provider is a typed-decision provider (`typesafe` or `openai-decisions`). Its
structured metrics door serves rows; the question door needs a chat model to write
the answer and returns 503 until one is configured. `dst plan` says so as a
`lens_model_unservable` warning. A lens served through Cube (`serving: cube`, or a
Cube-backed lens with no `serving:`) needs no chat model to apply either: an agent in driver mode (every MCP caller) writes its own Cube query and
dst calls no model for it, so a declared provider that cannot chat (embeddings only, or
no key) is the same warning, saying the HTTP question door without a driver returns
503. A lens that falls back to raw-SQL generation still needs its chat model, and the
same code is an error.
