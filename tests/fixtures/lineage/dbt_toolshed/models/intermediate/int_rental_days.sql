select r.rental_id, r.tool_id, r.member_id, r.rented_on, r.returned_on, r.due_on,
       t.day_rate, date_diff('day', r.rented_on, coalesce(r.returned_on, r.due_on)) as days
from {{ ref('stg_rentals') }} r
join {{ ref('stg_tools') }} t using (tool_id)
