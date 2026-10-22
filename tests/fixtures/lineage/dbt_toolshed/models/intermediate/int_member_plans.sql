select member_id, joined_on, upper(plan) as plan
from {{ ref('stg_members') }}
