MODEL (
  name staging.stg_tools,
  kind VIEW
);

SELECT tool_id, tool_name, category, day_rate
FROM raw.tools
