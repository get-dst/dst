MODEL (
  name staging.stg_rentals,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column rented_on
  ),
  grain rental_id,
  cron '@daily'
);

SELECT
  rental_id,
  tool_id,
  member_id,
  rented_on,
  returned_on,
  due_on
FROM raw.rentals
WHERE rented_on BETWEEN @start_ds AND @end_ds
