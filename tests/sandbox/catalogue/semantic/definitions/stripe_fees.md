---
metric: stripe_fees
summary: "The card processor's fee per charge; payouts arrive net of it. Sum fees from the order (one charge per card order), never from every raw charge version."
about: webshop_orders.fees_eur
aliases: [Stripe fees, card fees, card payment fees, processing fees, processor fees]
---
**Stripe fees** are what the card processor keeps on each card order (`fees_eur` on the order;
invoice orders have none). Payouts (`stripe_payouts`) arrive net of fees. The raw `raw_stripe.charge`
table keeps one row per sync version, so summing it directly overstates fees by about 4 %; the
order-level figure is the deduplicated one.
