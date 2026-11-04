-- Synthetic objects of the kinds the model keeps outside what ucstate manages:
-- a materialized view, a streaming table and a metric view, over one managed
-- table, and a view that reads the materialized view. ucstate leaves them alone,
-- and the explorer still draws them.
--
-- Run it with:
--   uv run python -m probes.run_sql packages/ucmodel/src/ucmodel/samples/kinds.sql --profile P
-- Everything lives in ucstate_test.kinds, which the second statement drops.

CREATE CATALOG IF NOT EXISTS ucstate_test COMMENT 'ucstate scenario and round-trip tests; safe to drop';
DROP SCHEMA IF EXISTS ucstate_test.kinds CASCADE;
CREATE SCHEMA ucstate_test.kinds COMMENT 'Object kinds outside the model, for the explorer';

CREATE TABLE ucstate_test.kinds.orders (
  order_id BIGINT NOT NULL COMMENT 'One row per order',
  region STRING,
  amount DECIMAL(10, 2),
  placed_at TIMESTAMP
) COMMENT 'Orders, one row each';

INSERT INTO ucstate_test.kinds.orders VALUES
  (1, 'north', 12.50, TIMESTAMP'2026-11-01 09:00:00'),
  (2, 'south', 40.00, TIMESTAMP'2026-11-01 10:30:00'),
  (3, 'north', 7.25, TIMESTAMP'2026-11-02 14:15:00');

CREATE MATERIALIZED VIEW ucstate_test.kinds.region_totals
COMMENT 'Sales by region, refreshed by its pipeline'
AS SELECT region, sum(amount) AS total, count(*) AS orders
FROM ucstate_test.kinds.orders
GROUP BY region;

CREATE STREAMING TABLE ucstate_test.kinds.orders_feed
COMMENT 'Orders as they arrive'
AS SELECT order_id, region, amount
FROM STREAM(ucstate_test.kinds.orders);

CREATE VIEW ucstate_test.kinds.order_metrics
WITH METRICS
LANGUAGE YAML
COMMENT 'Order measures by region'
AS $$
version: 1.1
source: ucstate_test.kinds.orders
dimensions:
  - name: region
    expr: region
measures:
  - name: revenue
    expr: SUM(amount)
  - name: order_count
    expr: COUNT(1)
$$;

CREATE VIEW ucstate_test.kinds.top_regions
COMMENT 'Regions above ten in sales'
AS SELECT region, total
FROM ucstate_test.kinds.region_totals
WHERE total > 10;
