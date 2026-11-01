CREATE VIEW sc_create_objects.customer_totals (
  customer_id COMMENT 'Customer key',
  total
)
COMMENT 'Order totals per customer'
AS SELECT c.customer_id, sum(o.amount) AS total
FROM sc_create_objects.customers AS c
JOIN sc_create_objects.orders AS o ON o.customer_id = c.customer_id
GROUP BY c.customer_id;

ALTER TABLE sc_create_objects.customer_totals ALTER COLUMN customer_id SET TAGS ('pii' = 'no');
GRANT SELECT ON TABLE sc_create_objects.customer_totals TO `account users`;
