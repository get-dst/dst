---
metric: receivables
summary: "Unpaid invoice balances; overdue = past due date with a balance. Month-end snapshots in ar_aging_monthly (to May 2026), today's balances on fi_invoices."
about: fi_invoices.outstanding_eur
aliases: [receivables, AR, accounts receivable, outstanding, overdue, owed]
---
**Receivables** are unpaid invoice balances (`outstanding_eur`). An invoice is **overdue** when
its due date has passed and a balance remains (`outstanding_eur > 0 AND due_date < today`).
"Right now" questions read `fi_invoices`; month-end questions read `ar_aging_monthly`, whose last
snapshot is 31 May 2026. Finnish books only; Bergström's unpaid invoices are the Fortnox
`Obetald`/`Delbetald` statuses.
