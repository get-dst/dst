---
metric: win_rate
summary: "Won quotes over decided quotes (won + lost + expired), by decision date."
about: quotes.quote_win_rate
aliases: [win rate, hit rate, quote conversion, quote win rate]
---
**Win rate** = won quotes / (won + lost + expired), counted by the quote's **decision date** in
the asked window. "Sent" in the question switches to sent date. Expired quotes count as not won.
For leads, lead-to-won is a different ratio (`crm_leads.crm_lead_win_rate`).
