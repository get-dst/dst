---
metric: emergency_callout
summary: "An unscheduled job, wo_type = 'Emergency callout', priced on labour with materials re-billed."
about: work_orders.emergency_callouts
aliases: [emergency callout, emergency callouts, emergency, emergencies, emergency job, emergency jobs]
sql: work_orders.wo_type = 'Emergency callout'
---
An **emergency callout** is a work order with `wo_type = 'Emergency callout'`: unscheduled, usually
same day. Planned maintenance is `wo_type = 'Maintenance visit'`. Zendesk tickets also have an
"Emergency" category, which is a support ticket, not a job.
