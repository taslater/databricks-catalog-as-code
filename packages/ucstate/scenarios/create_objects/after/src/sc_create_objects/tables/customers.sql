CREATE TABLE sc_create_objects.customers (
  customer_id BIGINT NOT NULL COMMENT 'Surrogate key',
  email STRING MASK sc_create_objects.mask_email,
  region STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
COMMENT 'One row per customer'
WITH ROW FILTER sc_create_objects.eu_only ON (region);

ALTER TABLE sc_create_objects.customers SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE sc_create_objects.customers TO `account users`;
