select tool_id, tool_name, category, day_rate
from {{ source('raw', 'tools') }}
