MERGE INTO core.customers AS t
USING (
  SELECT c.contact_id AS customer_id, c.contact_id, c.web_account_id, c.loyalty_no, c.region,
    least(c.created_at, a.signup_at) AS first_seen
  FROM raw_crm.contacts AS c
  LEFT JOIN raw_web.accounts AS a ON a.account_id = c.web_account_id
) AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.web_account_id = s.web_account_id, t.loyalty_no = s.loyalty_no,
  t.region = s.region
WHEN NOT MATCHED THEN INSERT (customer_id, contact_id, web_account_id, loyalty_no, region,
  first_seen)
  VALUES (s.customer_id, s.contact_id, s.web_account_id, s.loyalty_no, s.region, s.first_seen);

INSERT OVERWRITE core.orders
SELECT concat('web-', o.order_id) AS order_key, 'web' AS channel, o.order_id AS source_id,
  c.customer_id, 0 AS store_id, o.placed_at AS ordered_at, o.currency
FROM raw_web.orders AS o
LEFT JOIN core.customers AS c ON c.web_account_id = o.account_id
WHERE o.status <> 'canceled'
UNION ALL
SELECT concat('pos-', t.txn_id), 'store', t.txn_id, c.customer_id, t.store_id, t.txn_ts, 'USD'
FROM raw_pos.transactions AS t
LEFT JOIN core.customers AS c ON c.loyalty_no = t.loyalty_no;

INSERT OVERWRITE core.order_lines
SELECT concat('web-', i.order_id) AS order_key, i.item_no AS line_no, i.sku, i.qty AS quantity,
  i.price AS unit_price, i.discount, CAST(0 AS DECIMAL(10,2)) AS tax
FROM raw_web.order_items AS i
UNION ALL
SELECT concat('pos-', l.txn_id), l.line_no, l.sku, l.qty, l.unit_price,
  CAST(0 AS DECIMAL(10,2)), l.tax
FROM raw_pos.transaction_lines AS l;

INSERT OVERWRITE core.refunds
SELECT r.refund_id, concat('pos-', r.txn_id) AS order_key, r.refunded_at, r.amount
FROM raw_pos.refunds AS r;
