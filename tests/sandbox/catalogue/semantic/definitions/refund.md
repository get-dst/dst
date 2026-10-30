---
metric: refund
summary: "Money returned to a webshop customer, a separate row dated by refund date with a reason; refund rate = refunded / gross order value for orders placed in the window."
about: webshop_orders.refunds_eur
aliases: [refund, refunds, returns, return rate, refund rate, refunded]
---
A **refund** is a separate row in Shopify (`webshop_refunds`: date, amount incl. VAT, reason -
returned, wrong item, damaged, customer changed mind). On the order it is `refunds_eur`.
**Refund rate** = refunds / gross order value of the orders placed in the window. A "return" is a
refund with reason returned; by product the share is by refund amount against line revenue, since
refunds are not itemised.
