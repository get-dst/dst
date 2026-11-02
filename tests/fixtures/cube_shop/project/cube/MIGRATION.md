# Migration report: dst catalogue -> Cube model

Written by `dst migrate-model`. The model is generated once and then authored: a re-run refuses to overwrite a file edited since.

## Census

| item | n |
|---|---:|
| dst entities | 4 |
| dst relationships | 3 |
| dst metrics | 12 |
| dst fields | 26 |
| dst definitions | 4 |
| dst lenses | 1 |
| dst blueprints | 1 |
| cube ambiguous terms | 1 |
| cube cubes | 4 |
| cube cubes (distinct-grain companions) | 1 |
| cube cubes with population baked | 1 |
| cube cubes with sql | 1 |
| cube cubes with versions dedup | 1 |
| cube definitions as segments | 1 |
| cube definitions in overlay (prose) | 2 |
| cube dimensions | 26 |
| cube dimensions renamed (name shared with a measure) | 1 |
| cube dimensions with a dictionary | 6 |
| cube fact view joins past one hop | 2 |
| cube fact views (joined dimensions) | 4 |
| cube hide (public false) | 1 |
| cube internal (public false) | 3 |
| cube joins | 3 |
| cube joins (distinct-grain companions) | 1 |
| cube measure filters | 2 |
| cube measures | 11 |
| cube measures count | 2 |
| cube measures derived (number) | 1 |
| cube measures ratio (number) | 1 |
| cube measures sum | 7 |
| cube measures sum_distinct_on -> second cube | 1 |
| cube measures with their own clock | 1 |
| cube segments | 1 |
| cube text-month time dimensions | 1 |
| cube time dimensions | 5 |
| cube views | 1 |

## Carried outside the Cube model

| where | item | how it is carried | failure mode |
|---|---|---|---|
| order_lines | sum_distinct_on ['order_discount'] | second cube order_lines_by_order_id at the distinct grain | wrong_grain |
| orders | primary_key ['order_id', '_synced'] | -> ['order_id'] (versions dedup keeps the latest sync per key) | wrong_grain |
| orders | field tips_eur shares its name with a metric | dimension renamed tips_eur_value (one namespace per cube) | not_declared |
| lens sales | untyped_fallback | DROPPED: a serving-config key the Cube lens has no use for | not_expressible |
| lens sales | connections | DROPPED: a serving-config key the Cube lens has no use for | not_expressible |

## Not native to Cube, carried as meta.dst or overlay (by design)

| how it is carried | failure mode without it |
|---|---|
| `additivity: level` + `snapshot_grain`: Cube sums a level across months; dst must pin the latest snapshot per bucket (meta.dst). | wrong_grain |
| `agg_time_field` (a measure's own clock): Cube applies whichever time dimension the query names; `meta.dst.clock` tells dst which one to name. | window_misbound |
| `default_time_field`: Cube has no default time dimension; `overlay.cubes.<cube>.default_time_dimension`. | window_misbound |
| `pinned_dimensions`: no Cube equivalent (overlay). | wrong_grain |
| `use_cases` / `common_questions` / blueprint `caveats`: routing text, overlay per cube. | unroutable |
| `internal` / `hide`: `public: false` on the cube, and no view lists the member; a hide with `except_groups` is listed under the view's `access_policy`, granted to those groups only (Cube's `contextToGroups` reads `security_context.dst.groups`), and overlay.hide. | hidden_column |
| Lens scope/policy (`not_for`, `not_computable`, `sources`, `access`, `expose`, `timezone`, `stale_after_days`): overlay.lenses. | scope_violation |
| Definitions without `sql`, ambiguous terms with readings/audiences: overlay.definitions / overlay.ambiguous. | ambiguous_metric |
| Certified answers stay SQL pairs in `lenses/<lens>/certified_answers.yaml`. | not_expressible |
| dst `fields` vs `dimensions`: both are Cube dimensions; the group-by hint is only the presence of `meta.dst.aliases`. | wrong_grain |
| Composite primary keys: every key column carries `primary_key: true`. | wrong_grain |
| View members are prefixed with the cube name (`prefix: true`, `<view>.<cube>_<member>`) only in a view where a member name occurs in two of its cubes (`month`, `entity`, the join key); a view whose cubes share no member name lists them bare (`<view>.<member>`). | not_declared |
| A Cube view resolves one join tree: a lens becomes `<lens>_lens` (scope, direct includes) plus one `<lens>_<fact>` view per cube with in-lens joins (the fact + the dimensions of every cube its many_to_one / one_to_one joins reach, by the first path found). | not_expressible |
| Primary keys are public (Cube hides them by default); an `internal` or hidden key stays `public: false`. | hidden_column |
