---
metric: ticket
summary: "A Zendesk case; categories Scheduling, Invoice question, Warranty claim, Emergency, Webshop order, Complaint, Other; open = status open or pending. Tickets carry no company."
about: tickets
aliases: [ticket, tickets, support ticket, support tickets, case, cases, support request]
---
A **ticket** is a Zendesk case (`tickets`). "Open" means status open or pending. First response
time is `first_response_minutes`; resolution time is `resolved_at - created_at`. Tickets link to a
CRM customer, a webshop order or a work order, but never to a company: Nordlund LVI vs Bergström
splits of tickets or CSAT are not computable, and Bergström's support does not run through this
Zendesk.
