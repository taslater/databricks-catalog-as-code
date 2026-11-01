-- ucstate: rebuildable
CREATE TABLE sc_rebuildable.totals COMMENT 'Per customer'
AS SELECT customer_id, sum(amount) AS amount FROM sc_rebuildable.orders GROUP BY customer_id;
