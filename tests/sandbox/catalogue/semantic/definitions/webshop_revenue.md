---
metric: webshop_revenue
summary: "Webshop order value ex-VAT by order date (UTC) - the group's webshop revenue; gross and net-of-refunds are different, named readings."
about: webshop_orders.net_of_vat_eur
aliases: [webshop revenue, webshop sales, online sales, verkkokauppa revenue, shop revenue]
---
**Webshop revenue** is the ex-VAT order value (`net_of_vat_eur`) of paid orders, dated by the
order's UTC timestamp. Shopify's order total and `marts.fct_orders.total_eur` are **gross** -
including 25.5 % VAT - and `fct_orders`' documentation saying "net of refunds" is wrong: it is
gross before refunds. Refunds are separate rows; "net of refunds" is gross minus refunds with VAT
still in. The BI tool's `webshop_revenue` tile sums the gross total and calls it net; do not
reproduce it. Finnish-time months (`order_month_local`) differ slightly because late-evening UTC
orders fall on the next local day.
