---
metric: fx_method
summary: "EUR/SEK conversion - actuals at the monthly average rate, the pack and balance sheet at the closing rate, the budget at the planning rate 10.80."
about: fx_rates
aliases: [exchange rate, FX rate, EUR/SEK, SEK conversion, planning rate, closing rate, average rate]
---
Bergström Rör books in SEK. Group policy converts P&L actuals at the **monthly average** rate,
the balance sheet and the **month-end pack** at the **closing** rate, and the 2026 budget was
built at the **planning rate 10.80**. `int_fx_rates` carries all three per month; months finance
has not published fall back to the planning rate (`is_fallback`), which no month does today.
When a question wants SEK, read `bergstrom_invoices`; when it wants EUR, read
`group_revenue_monthly` (average).
