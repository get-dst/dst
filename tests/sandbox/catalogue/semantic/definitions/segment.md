---
metric: segment
summary: "Customer segment is Households, Housing companies or Commercial & public, read from the transaction (quote, deal, lead, invoice) - not from the customer dimension's legacy label."
about: quotes.segment
aliases: [segment, customer segment, by segment]
value_aliases:
  household: Households
  households: Households
  consumers: Households
  private customers: Households
  housing company: Housing companies
  housing companies: Housing companies
  taloyhtiö: Housing companies
  brf: Housing companies
  commercial: Commercial & public
  public: Commercial & public
  commercial and public: Commercial & public
  b2b: Commercial & public
---
**Segment** has three values: Households, Housing companies, Commercial & public. Read it from the
transaction table (quotes, bookings, leads, invoices, partner leads all carry it cleanly). The
customer dimension's `primary_segment` holds historical labels with inconsistent spellings
("Housing company", "Property manager", "Commercial") and is never a breakdown. Property managers
are housing-company customers.
