CREATE TABLE sc_create_objects.orders (
  order_id BIGINT NOT NULL,
  customer_id BIGINT,
  amount DECIMAL(10,2),
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customers_fk FOREIGN KEY (customer_id)
    REFERENCES sc_create_objects.customers (customer_id)
)
CLUSTER BY (customer_id);

ALTER TABLE sc_create_objects.orders ADD CONSTRAINT amount_positive CHECK (amount > 0);
