# Designing tables dst can answer from

Everything else in these docs assumes the warehouse exists and teaches you to
*describe* it. This page is for the other direction: which table shapes make a
lens reliable, and which ones guarantee a class of wrong answer no
verification check can catch — because the SQL is sound, the numbers ground,
and only the *meaning* is wrong. Audited across a production project, most
wrong answers that survived every check traced to one of the shapes below.

Two rules frame everything here:

- **Prose in a `description` is advisory.** Measured repeatedly: a rule
  written in prose is obeyed for one phrasing and violated for the next. If a
  shape needs a rule, the rule belongs in a rail — `population_filter`,
  `pinned_dimensions`, metric `filters`, a definition's `sql:`.
- **Some shapes are better fixed upstream than declared around.** A rail is a
  patch on a shape; a remodel removes the class. When the table below says
  *remodel*, the cheapest reliable fix is a dbt model, not a dst declaration.

## The shapes, their failure signatures, and the mechanism

| shape | failure signature if unhandled | the dst mechanism | or remodel |
|---|---|---|---|
| daily-snapshot fact table | totals grow with history; confident zeros when a hardcoded today misses a lagging partition | describe in `grain`; every "now" metric pins `MAX(date)` via metric `filters` | a `_current` view with one row per entity |
| semi-additive column repeated across a grid | a figure several multiples too high — `SUM` is *always* wrong | `pinned_dimensions` (coarse: it demands a pin or group, it cannot check a range filter) | **preferred**: one row per (entity, period) in its own table |
| multi-variant rows keyed by a discriminator | exact-multiple errors — every query must pin the discriminator or it multi-counts | mandatory metric `filters` pinning one value | split the variants into separate models |
| current-state column copied onto historical rows | flat or nonsense trends nobody questions — trending it is meaningless but syntactically fine | say so in the field description AND never expose it as a trend metric | move it to the dimension table where it belongs |
| dense / zero-filled grid | zero-vs-missing confusion in both directions — a zero means "no activity" here and "no data" in a sparse table | state which convention holds, per entity, in `grain` | — (the convention is the design; just declare it) |
| scope-narrowed column beside company-wide ones (`*_pms`, `*_emea`) | a subset served as the company total — measured at ~40% understatement with every check passing | a decisive definition naming the trap in negative form, or `not_computable` for the bare question | rename so the scope is in the name every reader sees |
| sentinel-dominated dimension (`(not set)`, `''`, far-future dates) | breakdowns with one meaningless bucket; inflated unbounded totals | `dst probe` value dictionaries arm `value_guard`; `population_filter` bounds out-of-range dates | clean upstream — a sentinel is a null wearing a costume |
| multi-currency money with no pin | a cross-currency sum denominated in nothing | `pinned_dimensions: [currency]` | store one reporting currency alongside |
| the same measure in two tables | the same question answered two ways depending on phrasing — a bound declared on one table is bypassed by the other | declare `population_filter` / the definition on **every** entity exposing it | keep one canonical carrier |
| placeholder metric (no `agg`, no `expr`) | generation improvises the ratio and gets the grain wrong (a period total served as a period average) | fill it in, or delete it — an empty metric is a trap, not a stub | — |
| undeclared period conventions | ~1% errors, no failing check, nobody notices ("last week": calendar week or trailing seven days?) | a decisive definition with `sql:` encoding the convention | — (this is a ruling; someone must make it) |

## The compounding case

The most instructive real failure stacked three shapes on one table: a daily
grid carrying a monthly quota copied onto every row, dense-filled for every
on-roster rep. The entity's own description said "never SUM across days" —
and generation summed across days anyway, twice, with two different wrong
magnitudes, because prose is advisory. The remodel — one row per rep-month —
would have removed the entire class before any lens existed.

Shapes also interact with each other's mechanisms: if a `current_x` metric
pins `MAX(date)`, do not define a `total_x` over the **same expression** with
an incompatible mandatory filter — the guard resolves such twins by the
question's wording, and a question naming neither is rejected with the
conflict spelled out. Give the second metric its own expression or its own
entity.

## Reviewing a warehouse before building on it

Almost every shape above is detectable from what `dst introspect --profile`
and `dst probe` already collect — column types, row counts, partitioning,
value dictionaries. The scaffolded **dst-warehouse-review** skill runs that
review: per-table findings, each naming the shape, the evidence, and whether
the remedy is a rail, a definition, or a remodel. Run it after connecting a
warehouse and before authoring the semantic layer; it is much cheaper to move
a semi-additive column than to discover it through a wrong board number.
