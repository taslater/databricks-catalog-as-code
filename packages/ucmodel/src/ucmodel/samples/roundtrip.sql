-- Synthetic objects for the Phase 1 round trip: build them, `ucstate import` them,
-- rebuild the import in the shadow, and compare. Each statement below is one
-- feature the model has to carry, and the ALTERs after the CREATEs make the
-- tables evolved rather than fresh, as a real target would be.
--
-- Run it with:
--   uv run python -m probes.run_sql packages/ucmodel/src/ucmodel/samples/roundtrip.sql
-- Everything lives in ucstate_test.roundtrip, which the second statement drops.

CREATE CATALOG IF NOT EXISTS ucstate_test COMMENT 'ucstate scenario and round-trip tests; safe to drop';
DROP SCHEMA IF EXISTS ucstate_test.roundtrip CASCADE;
CREATE SCHEMA ucstate_test.roundtrip COMMENT 'Round-trip coverage for ucstate import';
ALTER SCHEMA ucstate_test.roundtrip SET TAGS ('purpose' = 'test');
GRANT USE SCHEMA ON SCHEMA ucstate_test.roundtrip TO `account users`;

CREATE FUNCTION ucstate_test.roundtrip.mask_email(email STRING)
RETURNS STRING
COMMENT 'Keeps only the domain'
RETURN concat('***@', substring_index(email, '@', -1));

CREATE FUNCTION ucstate_test.roundtrip.visible_region(region STRING)
RETURNS BOOLEAN
RETURN region <> 'hidden';

CREATE FUNCTION ucstate_test.roundtrip.add_tax(
  amount DOUBLE,
  rate DOUBLE DEFAULT 0.2 COMMENT 'Tax rate as a fraction'
)
RETURNS DOUBLE
RETURN amount * (1 + rate);

CREATE FUNCTION ucstate_test.roundtrip.slugify(s STRING)
RETURNS STRING
LANGUAGE PYTHON
AS $$
return s.lower().replace(' ', '-')
$$;

-- A table with identity, a default, a mask, a row filter and column mapping.
CREATE TABLE ucstate_test.roundtrip.customers (
  customer_id BIGINT GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1),
  email STRING COMMENT 'Contact address',
  region STRING DEFAULT 'EU',
  full_name STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
COMMENT 'One row per customer'
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name',
  'my.example' = 'yes'
);
ALTER TABLE ucstate_test.roundtrip.customers ALTER COLUMN email SET MASK ucstate_test.roundtrip.mask_email;
ALTER TABLE ucstate_test.roundtrip.customers SET ROW FILTER ucstate_test.roundtrip.visible_region ON (region);
INSERT INTO ucstate_test.roundtrip.customers (email, region, full_name)
VALUES ('ada@example.com', 'EU', 'Ada'), ('grace@example.com', 'US', 'Grace');
ALTER TABLE ucstate_test.roundtrip.customers RENAME COLUMN full_name TO name;
ALTER TABLE ucstate_test.roundtrip.customers SET TAGS ('domain' = 'sales');
ALTER TABLE ucstate_test.roundtrip.customers SET TAGS ('reviewed');
ALTER TABLE ucstate_test.roundtrip.customers ALTER COLUMN email SET TAGS ('pii' = 'email');
GRANT SELECT ON TABLE ucstate_test.roundtrip.customers TO `account users`;

-- A table with an FK, a generated column, a CHECK and liquid clustering,
-- then a column added with a default, a widened type and a reorder.
CREATE TABLE ucstate_test.roundtrip.orders (
  order_id BIGINT NOT NULL,
  customer_id BIGINT,
  amount DECIMAL(10, 2) NOT NULL,
  amount_doubled DECIMAL(12, 2) GENERATED ALWAYS AS (amount * 2),
  quantity INT,
  order_date DATE,
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customers_fk FOREIGN KEY (customer_id)
    REFERENCES ucstate_test.roundtrip.customers (customer_id)
)
CLUSTER BY (order_date, customer_id)
COMMENT 'One row per order';
INSERT INTO ucstate_test.roundtrip.orders (order_id, customer_id, amount, quantity, order_date)
VALUES (10, 1, 5.00, 1, DATE'2026-10-01'), (11, 2, 7.50, 3, DATE'2026-10-02');
ALTER TABLE ucstate_test.roundtrip.orders ADD CONSTRAINT amount_positive CHECK (amount > 0);
ALTER TABLE ucstate_test.roundtrip.orders ADD COLUMN status STRING;
ALTER TABLE ucstate_test.roundtrip.orders SET TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.enableTypeWidening' = 'true'
);
ALTER TABLE ucstate_test.roundtrip.orders ALTER COLUMN status SET DEFAULT 'new';
ALTER TABLE ucstate_test.roundtrip.orders ALTER COLUMN status COMMENT 'Order status';
ALTER TABLE ucstate_test.roundtrip.orders ALTER COLUMN quantity TYPE BIGINT;
ALTER TABLE ucstate_test.roundtrip.orders ALTER COLUMN order_date AFTER order_id;

-- Partitioning, reserved and mixed-case names, complex types, awkward text.
CREATE TABLE ucstate_test.roundtrip.order_lines (
  `order` BIGINT NOT NULL COMMENT 'O\'Brien\'s order, with a \\ backslash',
  `Line Number` INT,
  event_date DATE,
  attrs MAP<STRING, STRING>,
  dims STRUCT<width: DOUBLE, `Height`: DOUBLE>,
  labels ARRAY<STRING>
)
PARTITIONED BY (event_date)
COMMENT 'Lines; partitioned by date'
-- A name with a space needs column mapping.
TBLPROPERTIES ('delta.columnMapping.mode' = 'name');

-- A table-valued function and a view that read the tables, with three-part
-- names the import has to turn into two-part ones.
CREATE FUNCTION ucstate_test.roundtrip.big_orders(threshold DECIMAL(10, 2))
RETURNS TABLE (order_id BIGINT, amount DECIMAL(10, 2))
RETURN SELECT order_id, amount FROM ucstate_test.roundtrip.orders WHERE amount > threshold;

CREATE VIEW ucstate_test.roundtrip.customer_orders (
  customer_id COMMENT 'Customer key',
  name,
  orders
)
WITH SCHEMA BINDING
COMMENT 'Orders per customer'
AS SELECT c.customer_id, c.name, count(o.order_id) AS orders
FROM ucstate_test.roundtrip.customers AS c
LEFT JOIN ucstate_test.roundtrip.orders AS o ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.name;
ALTER VIEW ucstate_test.roundtrip.customer_orders SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE ucstate_test.roundtrip.customer_orders TO `account users`;

CREATE VOLUME ucstate_test.roundtrip.landing COMMENT 'Raw files';
ALTER VOLUME ucstate_test.roundtrip.landing SET TAGS ('zone' = 'raw');
GRANT READ VOLUME ON VOLUME ucstate_test.roundtrip.landing TO `account users`;
