MODEL (
  name marts.dim_tools,
  kind FULL,
  grain tool_id
);

SELECT tool_id, tool_name, category, day_rate FROM staging.stg_tools
