---
metric: revenue
summary: "Invoiced sales ex-VAT by invoice date, in EUR at the monthly average rate; group revenue = the three operating entities minus intercompany."
about: group_revenue_monthly
aliases: [revenue, group revenue, sales revenue, turnover, net revenue ex-VAT]
sources: [intermediate.int_revenue_by_entity_month]
---
Revenue is invoiced sales **excluding VAT**, dated by invoice date, in EUR. Finland comes from
the Netvisor general ledger (journal_source = NETVISOR only); Bergström Rör from the Fortnox
export converted at the **monthly average** EUR/SEK rate (group policy for the P&L; the closing
rate is for the balance sheet and the month-end pack); the webshop from Shopify orders **net of
VAT** (the order total Shopify shows includes 25.5 % VAT).

Group revenue = Nordlund LVI Oy + Bergström Rör AB + Nordlund Verkkokauppa, with the
**Intercompany** stream (Nordlund LVI selling materials to Bergström) taken out - the
`group_revenue_monthly` entity already excludes it. The **MIGRATION** journal batch (Bergström's
opening balances loaded into the Finnish ledger, 1,654,711.50 EUR) is not revenue and is excluded
upstream; never sum `raw_netvisor.gl_entry` as revenue.

Not revenue: bookings (signed contract value, sales lens), cash collected, the Stripe payout, the
gross order total incl. VAT, and anything from `legacy_marts.mart_revenue_v1` (frozen at
2026-03-31, Finland only, webshop gross of VAT) or `marts.mart_finance_revenue` (FI books only).
