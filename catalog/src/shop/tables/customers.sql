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
