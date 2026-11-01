CREATE TABLE probe_lineage.orders (
  order_id BIGINT NOT NULL,
  customer_id BIGINT,
  amount DECIMAL(10,2),
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customers_fk FOREIGN KEY (customer_id)
    REFERENCES probe_lineage.customers (customer_id)
)
CLUSTER BY AUTO;
