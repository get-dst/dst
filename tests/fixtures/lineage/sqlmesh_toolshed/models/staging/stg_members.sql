MODEL (
  name staging.stg_members,
  kind FULL,
  description 'One row per member (comments; and parentheses) in the header'
);

WITH latest AS (
  SELECT member_id, joined_on, plan FROM raw.members
)
SELECT member_id, joined_on, plan FROM latest
