---
metric: contract_churn
summary: "A maintenance contract not renewed at its end date; the one churn so far (62,600 EUR) ended 2026-06-01 and the monthly mart books it in May by decision date."
about: maintenance_contracts.churned_contracts
aliases: [contract churn, churned contracts, contracts churned, non-renewal, not renewed, lost contracts]
sql: maintenance_contracts.status = 'Churned'
---
**Contract churn** is a maintenance contract that was not renewed (`status = 'Churned'`). The
only one so far: one customer, 62,600 EUR annual value, contract ended 2026-06-01; the renewal
decision was recorded 2026-05-31, so `contract_renewals_monthly` books it in **May** while the
contract itself lapsed in **June**. "Contracts churned in Q2" is 1 either way; a by-month split
must say which date it uses.
