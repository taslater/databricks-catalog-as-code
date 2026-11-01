CREATE VIEW sc_replace_view_function.open_orders (
  order_id COMMENT 'Order key',
  amount
)
COMMENT 'Open orders'
AS SELECT order_id, amount FROM sc_replace_view_function.orders WHERE status = 'open';

ALTER VIEW sc_replace_view_function.open_orders SET TAGS ('domain' = 'sales');
ALTER TABLE sc_replace_view_function.open_orders ALTER COLUMN order_id SET TAGS ('key' = 'yes');
GRANT SELECT ON TABLE sc_replace_view_function.open_orders TO `account users`;
