---
metric: work_order
summary: "A FieldFlow job - Emergency callout or Maintenance visit - from the latest full sync batch; statuses Scheduled, In progress, Completed, Invoiced, Cancelled."
about: work_orders
aliases: [work order, work orders, job, jobs, service job, callout]
---
A **work order** is a job in FieldFlow (`work_orders`): an **emergency callout** (unscheduled,
priced on labour, materials re-billed) or a **maintenance visit** (a visit under a maintenance
contract). Dated by `completed_date`; scheduled and in-progress jobs have none. Only the
B2026-08-31 batch is read - the July batch was a partial sync.
