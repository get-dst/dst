select
    id              as payment_id,
    order_id,
    payment_method,
    amount / 100.0  as amount
from {{ ref('raw_payments') }}
