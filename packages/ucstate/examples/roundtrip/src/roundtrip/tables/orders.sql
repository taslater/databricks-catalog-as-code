CREATE TABLE roundtrip.orders (
  order_id BIGINT NOT NULL,
  order_date DATE,
  customer_id BIGINT,
  amount DECIMAL(10,2) NOT NULL,
  amount_doubled DECIMAL(12,2) GENERATED ALWAYS AS (amount * 2),
  quantity BIGINT,
  status STRING DEFAULT 'new' COMMENT 'Order status',
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customers_fk FOREIGN KEY (customer_id)
    REFERENCES roundtrip.customers (customer_id)
)
CLUSTER BY (order_date, customer_id)
COMMENT 'One row per order'
TBLPROPERTIES (
  'delta.enableTypeWidening' = 'true',
  'delta.feature.allowColumnDefaults' = 'supported'
);

ALTER TABLE roundtrip.orders ADD CONSTRAINT amount_positive CHECK (amount > 0);
