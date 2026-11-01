CREATE VIEW roundtrip.customer_orders (
  customer_id COMMENT 'Customer key',
  name,
  orders
)
WITH SCHEMA BINDING
COMMENT 'Orders per customer'
AS SELECT c.customer_id, c.name, count(o.order_id) AS orders
FROM roundtrip.customers AS c
LEFT JOIN roundtrip.orders AS o ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.name;

ALTER VIEW roundtrip.customer_orders SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE roundtrip.customer_orders TO `account users`;
