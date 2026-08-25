# Bootstrap from history

Thirty days of warehouse query history is a usage-weighted map of the org's real
semantic layer: which tables carry the business, what the metrics are called in the
org's own vocabulary, and where practice already disagrees with itself: the definition
wars your dashboards are already fighting, with run counts attached.

dst ships the mining of that map as a **skill, not a server feature**. `dst init`
scaffolds `.claude/skills/dst-history-bootstrap/` into every project
(`services/cli/init.py`); your driver agent does the reading and the judgment, and
dst's `plan`/`apply` gates catch what it gets wrong.

!!! clarify "Where the work happens"
    Generated BI SQL is not separable from authored practice by heuristics: pivot
    engines, semantic-layer compilers and scheduled jobs produce shape families that
    defeat any fixed rule, while an agent reading the export tells them apart on
    sight. So the reading and the judgment happen in your agent. dst supplies the
    history SQL, the landing sequence, and the gates that catch what the agent gets
    wrong.

## What the skill does

Three moves, then the landing sequence every authoring path shares.

**Pull shape-level history, metadata only.** The skill carries the SQL for the two
supported warehouses whose history catalogs expose a query-shape hash: BigQuery groups
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` by `query_info.query_hashes.normalized_literals`
(needs `bigquery.jobs.listAll`); Snowflake groups `account_usage.query_history` by
`query_parameterized_hash` (needs `IMPORTED PRIVILEGES` on the `SNOWFLAKE` database).
One row per query *shape* (literals folded), with a representative text, run count, and
distinct principals; shapes run once by a single principal are dropped. No table access
is needed, and the export stays **outside the repo**: query text can embed literals,
including personal data.

**Separate authored from generated.** Only authored SQL expresses judgment; generated
SQL is a tool *consuming* metrics, not defining them. The tells the skill teaches: dbt's
leading `{"app": "dbt"}` comment (ingest those models via `dst import dbt` instead
of mining them), BI pivot engines (`__mask` / `rowDepth` / `colDepth` projections,
`agg0_` aliases), semantic-layer compilers (`__with_t_0`-style generated CTEs),
and service accounts running one shape on a schedule. A generated
family counts as one consumer surface, however many filter permutations it ran.

**Extract.** Group the authored head by metric intent: what is measured, and what its
author *called* it. Aliases are the org's own vocabulary, and usage weights make the
entity shortlist. From that the agent drafts `semantic/`: entities for the load-bearing
tables (grain from observed keys and joins, fields the queries actually touch),
definitions for the recurring metrics; and where authored practice genuinely disagrees
(same metric, different filters, grain, or measure), a definition with
`status: ambiguous` and `possible_mappings` taken from the real variants, so dst
[asks instead of guessing](../concepts/clarify-and-refusal.md). Ask, don't crown: run
count is popularity, not correctness.

The most-run authored questions whose intent is unambiguous become
[certified-answer](../concepts/certified.md) candidates with
`source: "history:<shape_hash>"` provenance, and `verified_by` left empty, because a
certified answer is served verbatim, so it counts only once a human vouches. A shape
family whose runs differ only in benign literals becomes one certified template
rather than N frozen pairs; [Certified answers](../concepts/certified.md) carries
the template contract.

Then the landing sequence:

```bash
dst plan
dst apply --probe-certified
dst query <lens> "<one real question per drafted metric>"
```

Gotchas the skill pins: a literal-only difference can *be* the war (a 120- vs 180-day
window definition differs only in a literal the shape hash folded), so the agent reads
the variants' literals before declaring two statements equivalent; scheduled traffic
inflates run counts, so multi-principal shapes weigh higher; and the history export
never goes into git.

## How a driver agent runs it

There is nothing to install and no server endpoint. Claude Code discovers the skill in
`.claude/skills/` on its own: ask it to bootstrap the semantic layer from query
history; any other agent can be handed `SKILL.md` and follow it as a plain procedure
(the scaffolded `AGENTS.md` says exactly that). The history SQL runs over whatever path
reaches the warehouse: `bq` or `snowsql` from the agent's terminal, or a human runs it
in the console and hands back the export. It composes with the scaffold's other skills:
`dst-semantic` (authoring from introspection) and `dst-certify` (the template
shapes for certified candidates).

## What it yields

A draft the org can argue with instead of a blank page: the **metric map** (which
tables and metrics actually carry the business, usage-weighted); **definitions in the
org's own vocabulary**, with the genuine definition wars surfaced as `status: ambiguous`,
seeds for [curated context](../concepts/curated-context.md) and lens curation, not silent winner-picking;
and a **certified head with provenance**, waiting on a human `verified_by`. The apply
gates, the certified suite, and the behavioral pins are the safety net under all of it.

!!! clarify "Clarify"
    Scope, honestly: this skill is a one-shot bootstrap, run by your agent. It is not
    a standing re-audit. The skill's history SQL covers BigQuery and Snowflake;
    DuckDB has no history catalog, and the skill carries no Postgres or MySQL
    variant.
    — `services/cli/init.py`
