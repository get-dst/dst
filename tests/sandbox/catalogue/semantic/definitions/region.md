---
metric: region
summary: "Finnish region (maakunta) of a CRM customer - Uusimaa, Pirkanmaa, Pohjois-Pohjanmaa, Varsinais-Suomi, Keski-Suomi, Päijät-Häme - reached through the customer; partners' \"region\" is a city."
about: customers.region
aliases: [region, regions, maakunta, by region, area]
---
**Region** lives on the CRM customer (`customers.region`); quotes, leads, invoices, work orders
and tickets reach it through their `customer_id`. Partners carry a city in their `region` column
(Helsinki, Tampere, ...). Webshop orders carry no region at all - "webshop revenue by region" is
not computable.
