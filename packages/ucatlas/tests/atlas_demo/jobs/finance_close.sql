INSERT OVERWRITE finance.recognized_revenue
WITH web AS (
  SELECT concat('W', o.order_id) AS order_ref, 'web' AS channel,
    to_date(o.placed_at) AS recognized_on, sum(i.qty * i.price) AS gross,
    CAST(0 AS DECIMAL(14,2)) AS tax, CAST(0 AS DECIMAL(14,2)) AS refunded
  FROM raw_web.orders AS o
  JOIN raw_web.order_items AS i ON i.order_id = o.order_id
  WHERE o.status = 'paid'
  GROUP BY o.order_id, o.placed_at
),
store AS (
  SELECT concat('P', t.txn_id) AS order_ref, 'store' AS channel,
    to_date(t.txn_ts) AS recognized_on, t.total_gross AS gross,
    (SELECT sum(l.tax) FROM raw_pos.transaction_lines AS l WHERE l.txn_id = t.txn_id) AS tax,
    coalesce((SELECT sum(r.amount) FROM raw_pos.refunds AS r WHERE r.txn_id = t.txn_id), 0)
      AS refunded
  FROM raw_pos.transactions AS t
)
SELECT u.order_ref, u.channel, u.recognized_on, u.gross, u.tax, u.refunded,
  u.gross - u.tax - u.refunded AS net_revenue
FROM (SELECT * FROM web UNION ALL SELECT * FROM store) AS u;

INSERT OVERWRITE finance.monthly_close
SELECT trunc(r.recognized_on, 'MM') AS month_start, r.channel,
  sum(r.net_revenue) AS net_revenue
FROM finance.recognized_revenue AS r
GROUP BY trunc(r.recognized_on, 'MM'), r.channel;
