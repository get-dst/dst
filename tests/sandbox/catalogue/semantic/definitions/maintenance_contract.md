---
metric: maintenance_contract
summary: "A yearly service agreement (housing companies, property managers) billed quarterly or annually; its end_date is when it comes up for renewal."
about: maintenance_contracts
aliases: [maintenance contract, maintenance contracts, service agreement, service contracts, contract book]
---
A **maintenance contract** is a yearly service agreement in FieldFlow (`maintenance_contracts`),
with `annual_value_eur`, a `start_date` and an `end_date`. "Up for renewal", "expiring" and
"ending" all mean `end_date` in the asked window. Status Active = in force, Renewed = replaced by
a successor contract, Churned = not renewed. "Service contracts" revenue is the invoiced stream in
the finance lens; the contract book is here.
