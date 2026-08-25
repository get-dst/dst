with customers as (
    select * from {{ ref('stg_customers') }}
),

orders as (
    select * from {{ ref('stg_orders') }}
),

payments as (
    select * from {{ ref('stg_payments') }}
),

customer_orders as (
    select
        customer_id,
        min(order_date)                             as first_order_date,
        max(order_date)                             as most_recent_order_date,
        count(order_id)                             as number_of_orders
    from orders
    group by customer_id
),

customer_payments as (
    select
        orders.customer_id,
        sum(payments.amount)                        as total_amount
    from payments
    left join orders using (order_id)
    group by orders.customer_id
)

select
    customers.customer_id,
    customers.customer_name,
    customer_orders.first_order_date,
    customer_orders.most_recent_order_date,
    coalesce(customer_orders.number_of_orders, 0)  as number_of_orders,
    coalesce(customer_payments.total_amount, 0)     as customer_lifetime_value
from customers
left join customer_orders using (customer_id)
left join customer_payments using (customer_id)
