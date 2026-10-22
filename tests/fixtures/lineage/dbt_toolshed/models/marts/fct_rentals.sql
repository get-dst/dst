select rental_id, tool_id, member_id, rented_on, returned_on, due_on,
       days, days * day_rate as rental_fee
from {{ ref('int_rental_days') }}
