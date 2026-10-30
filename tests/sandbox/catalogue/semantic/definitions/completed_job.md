---
metric: completed_job
summary: "A work order counts as done when its status is Completed or Invoiced; Scheduled, In progress and Cancelled are not done."
about: work_orders.status
aliases: [completed jobs, completed work orders, done jobs, finished jobs, jobs completed]
sql: work_orders.status IN ('Completed', 'Invoiced')
---
A **completed** work order has status `Completed` or `Invoiced` (invoicing follows completion).
`Scheduled` and `In progress` are open; `Cancelled` never happened. Margin and billed amounts are
only meaningful on completed jobs, which is also where `completed_date` is set.
