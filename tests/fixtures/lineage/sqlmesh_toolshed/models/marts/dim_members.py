import typing as t
from datetime import datetime

import pandas as pd
from sqlmesh import ExecutionContext, model


@model(
    "marts.dim_members",
    kind="FULL",
    columns={"member_id": "int", "joined_on": "date", "plan": "text"},
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
    table = context.resolve_table("staging.stg_members")
    return context.fetchdf(f"SELECT member_id, joined_on, upper(plan) AS plan FROM {table}")
