MODEL (
  name marts.fct_rentals,
  kind FULL,
  grain rental_id
);

WITH rental_days AS (
  SELECT
    r.rental_id, r.tool_id, r.member_id, r.rented_on, r.returned_on, r.due_on, t.day_rate,
    DATE_DIFF('day', r.rented_on, COALESCE(r.returned_on, r.due_on)) AS days
  FROM staging.stg_rentals AS r
  JOIN staging.stg_tools AS t USING (tool_id)
)
SELECT rental_id, tool_id, member_id, rented_on, returned_on, due_on, days,
       days * day_rate AS rental_fee
FROM rental_days
