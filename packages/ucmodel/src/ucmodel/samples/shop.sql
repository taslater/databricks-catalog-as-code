-- The source of ucstate_prod.shop when shop.raw.json was read (2026-10-31):
-- catalog/src, in the order ucstate reads it. It uses two-part names, so it
-- runs with a catalog set on each request, as ucstate runs it. It is kept
-- here rather than read from catalog/, so that the sample and its source
-- change together.

-- src/shop/schema.sql
CREATE SCHEMA shop
COMMENT 'A small synthetic shop: customers, products and their orders';
ALTER SCHEMA shop SET TAGS ('domain' = 'sales');
GRANT USE SCHEMA ON SCHEMA shop TO `account users`;

-- src/shop/tables/customers.sql
CREATE TABLE shop.customers (
  customer_id BIGINT NOT NULL COMMENT 'Surrogate key',
  name STRING NOT NULL COMMENT 'Display name',
  email STRING COMMENT 'Contact address',
  phone STRING COMMENT 'Contact number',
  country STRING COMMENT 'ISO 3166-1 alpha-2 code',
  created_at TIMESTAMP COMMENT 'When the customer signed up',
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
COMMENT 'One row per customer';
ALTER TABLE shop.customers ALTER COLUMN email SET TAGS ('pii' = 'email');

-- src/shop/tables/order_lines.sql
CREATE TABLE shop.order_lines (
  order_id BIGINT NOT NULL COMMENT 'The order this line is on',
  line_no INT NOT NULL COMMENT 'Position on the order, from 1',
  product_id BIGINT NOT NULL COMMENT 'What was ordered',
  quantity INT NOT NULL COMMENT 'How many',
  unit_price DECIMAL(10,2) COMMENT 'Price paid per unit',
  CONSTRAINT order_lines_pk PRIMARY KEY (order_id, line_no),
  CONSTRAINT order_lines_order_fk FOREIGN KEY (order_id) REFERENCES shop.orders (order_id),
  CONSTRAINT order_lines_product_fk FOREIGN KEY (product_id) REFERENCES shop.products (product_id)
)
COMMENT 'One row per product on an order';

-- src/shop/tables/orders.sql
CREATE TABLE shop.orders (
  order_id BIGINT NOT NULL COMMENT 'Surrogate key',
  customer_id BIGINT NOT NULL COMMENT 'Who placed the order',
  ordered_at TIMESTAMP NOT NULL COMMENT 'When the order was placed',
  status STRING COMMENT 'placed, shipped or cancelled',
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES shop.customers (customer_id)
)
COMMENT 'One row per order';

-- src/shop/tables/products.sql
CREATE TABLE shop.products (
  product_id BIGINT NOT NULL COMMENT 'Surrogate key',
  sku STRING NOT NULL COMMENT 'Stock-keeping unit',
  name STRING NOT NULL COMMENT 'Display name',
  unit_price DECIMAL(10,2) COMMENT 'List price, in the shop currency',
  CONSTRAINT products_pk PRIMARY KEY (product_id)
)
COMMENT 'One row per product';
GRANT SELECT ON TABLE shop.products TO `account users`;

-- src/shop/views/order_totals.sql
CREATE VIEW shop.order_totals
COMMENT 'Each order with its total'
AS SELECT o.order_id, o.customer_id, o.ordered_at, sum(l.quantity * l.unit_price) AS total
FROM shop.orders o
JOIN shop.order_lines l ON l.order_id = o.order_id
GROUP BY o.order_id, o.customer_id, o.ordered_at;
GRANT SELECT ON TABLE shop.order_totals TO `account users`;
