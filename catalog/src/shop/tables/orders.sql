CREATE TABLE shop.orders (
  order_id BIGINT NOT NULL COMMENT 'Surrogate key',
  customer_id BIGINT NOT NULL COMMENT 'Who placed the order',
  ordered_at TIMESTAMP NOT NULL COMMENT 'When the order was placed',
  status STRING COMMENT 'placed, shipped or cancelled',
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) REFERENCES shop.customers (customer_id)
)
COMMENT 'One row per order';
