---
metric: margin
summary: "Job margin = (billed - labour - materials) / billed, full cost, from work_orders; the labour-only figure on fct_work_orders (53 % on emergencies) is wrong."
about: work_orders.job_margin_pct
aliases: [margin, margin rate, job margin, margin on service work, work order margin, service margin, callout margin]
---
**Margin** on service work is billed amount minus the full cost of the job (labour AND materials),
as a share of billed, computed over the asked jobs (sum over sum, never an average of row
percentages). The core mart `marts.fct_work_orders` and the BI `emergency_margin` tile omit
materials on emergency callouts and show about 53 %; the true emergency margin is about 23 %. Webshop product margin is a different thing (`webshop_order_lines`).
