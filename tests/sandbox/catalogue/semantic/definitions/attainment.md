---
metric: attainment
summary: "Ambiguous - actual over target, actual over budget, or a sales rep's bookings over quota; say which."
status: ambiguous
aliases: [attainment, attainment rate, "% of goal", percent of goal, goal attainment]
possible_mappings:
  - "actual revenue over the planning-sheet target - revenue_vs_target.attainment_vs_target (what teams are measured on)"
  - "actual revenue over the board budget - budget_vs_actual.attainment_vs_budget (the board view)"
  - "a sales rep's booked amount over quota, quarterly - Compass attainment table (sales lens, read-only)"
audiences:
  board: actual revenue over the board budget
  sales: a sales rep's booked amount over quota, quarterly
---
Three things are called attainment. Finance measures teams on **actual over target** (the
planning sheet, latest version); the board reads **actual over budget**; sales reads a rep's
**bookings over quota** per fiscal quarter in Compass. The ratios are over the asked window as
sums (sum of actuals / sum of targets), never an average of monthly percentages. A question that
names only "attainment" is asked which; "target", "budget" or "quota" in the question settles it.
