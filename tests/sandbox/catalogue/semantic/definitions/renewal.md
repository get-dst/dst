---
metric: renewal
summary: "A maintenance contract extended at its end date (a successor contract); renewal rate = renewed / (renewed + churned) among decided contracts."
about: contract_renewals_monthly.contract_renewal_rate
aliases: [renewal, renewals, renewed, renewal rate, contract renewal rate]
---
A **renewal** is a maintenance contract extended at its end date - FieldFlow records a successor
contract and marks the old one Renewed. **Renewal rate** = renewed / (renewed + churned) over the
contracts decided in the window. To date: 11 renewed, 1 churned (91.7 %). Renewal here is a
contract event; it is not customer churn, not subscription churn, and Compass "Renewal" deal_type
is a sales deal type.
