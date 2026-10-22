select member_id, joined_on, plan
from {{ source('raw', 'members') }}
