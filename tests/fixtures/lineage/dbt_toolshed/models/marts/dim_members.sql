select member_id, joined_on, plan
from {{ ref('int_member_plans') }}
