---
metric: gross
summary: "Gross means including VAT (25.5 % Finland, 25 % Sweden); webshop order totals are gross."
about: webshop_orders.gross_eur
aliases: [gross, including VAT, incl VAT, inc VAT, with VAT, order total]
---
**Gross** is the amount including VAT. Shopify's order total (`gross_eur`, `fct_orders.total_eur`)
is gross; the ex-VAT figure is `net_of_vat_eur`. Netvisor invoices carry both
(`gross_amount_eur` / `net_amount_eur`); Fortnox invoices carry `belopp_inkl_moms` /
`belopp_exkl_moms`. The Pro app funnel's `revenue_eur` is gross despite its documentation.
