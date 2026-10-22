select rental_id, tool_id, member_id, rented_on, returned_on, due_on
from {{ source('raw', 'rentals') }}
