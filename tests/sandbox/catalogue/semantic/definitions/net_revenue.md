---
metric: net_revenue
summary: "Ambiguous - \"net\" means ex-VAT in finance usage, but older webshop reports used it for net of refunds."
status: ambiguous
aliases: [net revenue, net sales, net]
possible_mappings:
  - "revenue excluding VAT - group_revenue_monthly.revenue_eur (finance: the standard meaning)"
  - "webshop sales net of refunds, still including VAT - webshop_orders.net_of_refunds_eur (the old webshop report)"
audiences:
  finance: revenue excluding VAT
  controller: revenue excluding VAT
  group finance: revenue excluding VAT
---
Finance means **revenue excluding VAT** when it says net revenue. The 2025 webshop dashboards used
"net" for **net of refunds** (gross order value minus refunds, VAT still in), and the BI tool's
`net_revenue` tile still points at the frozen `legacy_marts.mart_revenue_v1`, which returns nothing
after March 2026 and carries the webshop gross of VAT. A question that just says "net revenue"
is asked which reading it wants; "ex-VAT" or "net of refunds" in the question settles it.
