---
metric: webshop_order
summary: "One checkout on Nordlundshop.fi (paid), timestamped in UTC; channel b2c_card = consumers paying by card, b2b_invoice = installers and businesses paying by invoice."
about: webshop_orders.channel
aliases: [webshop order, webshop orders, online order, online orders, checkout, checkouts, card customers, invoice customers, installer customers, business customers, consumer orders]
value_aliases:
  card: b2c_card
  card customers: b2c_card
  consumers: b2c_card
  households: b2c_card
  b2c: b2c_card
  invoice: b2b_invoice
  invoice customers: b2b_invoice
  installers: b2b_invoice
  installer customers: b2b_invoice
  business customers: b2b_invoice
  b2b: b2b_invoice
---
A **webshop order** is one paid checkout (`webshop_orders`), dated by its UTC timestamp (a few
late-evening orders fall on the next day in Finnish time; `order_month_local` is the Finnish
month). `channel` says who ordered: `b2c_card` are consumers paying by card, `b2b_invoice` are
installers and business customers paying by invoice. The webshop stores no region or product
category on the order; categories come from order lines.
