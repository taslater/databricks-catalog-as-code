CREATE VIEW shop.order_totals
COMMENT 'Each order with its total'
AS SELECT o.order_id, o.customer_id, o.ordered_at, sum(l.quantity * l.unit_price) AS total
FROM shop.orders o
JOIN shop.order_lines l ON l.order_id = o.order_id
GROUP BY o.order_id, o.customer_id, o.ordered_at;

GRANT SELECT ON TABLE shop.order_totals TO `account users`;
