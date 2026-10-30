---
metric: intercompany
summary: "Nordlund LVI's material sales to Bergström - a revenue line in the Finnish books, eliminated in group figures."
about: group_revenue_monthly.revenue_stream
aliases: [intercompany, internal sales, intra-group, elimination]
---
**Intercompany** revenue is Nordlund LVI Oy selling materials to Bergström Rör AB. It is a stream
in the Finnish ledger and in `marts.mart_finance_revenue`; group revenue excludes it
(`group_revenue_monthly` filters it out). The monthly amounts are in
`finance_reporting.intercompany_sales`; ask finance for them, this layer does not serve them as
revenue.
