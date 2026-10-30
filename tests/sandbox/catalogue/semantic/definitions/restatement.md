---
metric: restatement
summary: "Today's books minus the month-end pack for the same month; positive = booked late."
about: revenue_restatements.restated_eur
aliases: [restatement, restated, late postings, late-booked, booked late]
---
A **restatement** is the difference between what the books say today and what the pack said at
close, per entity, stream and month. Positive means invoices were booked after the pack. For
Bergström rows a small restatement appears every month that is only FX (books at average rate,
pack at closing) - the dbt documentation saying "both sides at closing rate" is stale.
