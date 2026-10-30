---
metric: product_category
summary: "Webshop product category from the catalogue - Valves, Tools & consumables, Drain & sewer parts, PEX pipe & fittings, Thermostats & controls, Radiators, Water heaters, Circulation pumps."
about: webshop_products.category
aliases: [product category, categories, product group, category]
value_aliases:
  valves: Valves
  radiator valves: Valves
  tools: Tools & consumables
  consumables: Tools & consumables
  drain parts: Drain & sewer parts
  sewer parts: Drain & sewer parts
  pex: PEX pipe & fittings
  pipe: PEX pipe & fittings
  fittings: PEX pipe & fittings
  thermostats: Thermostats & controls
  controls: Thermostats & controls
  radiators: Radiators
  water heaters: Water heaters
  heaters: Water heaters
  pumps: Circulation pumps
  circulation pumps: Circulation pumps
---
**Product category** is on the catalogue (`webshop_products.category`), reached from order lines
through `product_id`. Product revenue by category sums line totals (incl. VAT) one value per
distinct line, so the known duplicate lines and the catalogue's superseded sync
versions never inflate it. "Heat pump parts", "stock-outs" and "margin trend by SKU" over time
need data the webshop does not record (no stock levels, no heat-pump category).
