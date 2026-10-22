select tool_id, tool_name, category, day_rate
from {{ ref('stg_tools') }}
