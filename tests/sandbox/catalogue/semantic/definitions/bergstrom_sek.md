---
metric: bergstrom_sek
summary: "Bergström Rör figures in SEK come only from the Fortnox export (belopp_exkl_moms by fakturadatum); EUR figures from group_revenue_monthly at the average rate."
about: bergstrom_invoices.belopp_exkl_moms
aliases: [in SEK, SEK, kronor, Swedish kronor, Bergström in SEK]
---
Bergström Rör AB books in **SEK**. The Fortnox export (`bergstrom_invoices`) is the only SEK source:
`belopp_exkl_moms` is ex-VAT (25 % moms), dated `fakturadatum`. It is not staged in dbt and its
column names are Swedish. Fiscal years were July-June before the acquisition; `rakenskapsar`
labels them Fortnox's way ("2025/26 (förkortat)" is the shortened year to October 2025), so
"fiscal year" questions about Bergström need the label, not a calendar year. Bergström has no
quotes, deals, tickets or CRM data in the warehouse.
