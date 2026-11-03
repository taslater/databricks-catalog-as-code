INSERT OVERWRITE sales.order_facts
SELECT l.order_key, l.line_no, o.ordered_at, to_date(o.ordered_at) AS order_date, o.channel,
  o.customer_id, o.store_id, l.sku, p.category, l.quantity,
  l.quantity * l.unit_price - l.discount AS net_amount
FROM core.order_lines AS l
JOIN core.orders AS o ON o.order_key = l.order_key
JOIN ref.products AS p ON p.sku = l.sku;

INSERT OVERWRITE sales.daily_sales
SELECT to_date(o.ordered_at) AS sale_date, o.store_id, l.sku, sum(l.quantity) AS units,
  sum(l.quantity * l.unit_price + l.tax) AS revenue
FROM core.order_lines AS l
JOIN core.orders AS o ON o.order_key = l.order_key
WHERE o.channel = 'store'
GROUP BY to_date(o.ordered_at), o.store_id, l.sku;

INSERT OVERWRITE sales.customer_monthly
SELECT f.customer_id, trunc(f.order_date, 'MM') AS month_start,
  count(DISTINCT f.order_key) AS orders, sum(f.net_amount) AS revenue
FROM sales.order_facts AS f
WHERE f.customer_id IS NOT NULL
GROUP BY f.customer_id, trunc(f.order_date, 'MM');
