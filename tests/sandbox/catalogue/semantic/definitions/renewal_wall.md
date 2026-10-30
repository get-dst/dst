---
metric: renewal_wall
summary: "The active maintenance contracts whose end dates fall in the same quarter - Q1 2027 is the wall: 18 contracts, 1,520,000 EUR annual value."
about: maintenance_contracts.end_date
aliases: [renewal wall, the wall, Q1 2027 wall, contracts coming up for renewal]
---
The **renewal wall** is the set of active maintenance contracts that all end in one quarter. The
Q1 2027 wall is 18 active contracts with 1,520,000 EUR annual value (`status = 'Active'`,
`end_date` between 2027-01-01 and 2027-03-31, latest full batch). It is read from
`maintenance_contracts`, never from the frozen snapshots.
