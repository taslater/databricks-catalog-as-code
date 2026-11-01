CREATE TABLE roundtrip.customers (
  customer_id BIGINT NOT NULL GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1),
  email STRING COMMENT 'Contact address' MASK roundtrip.mask_email,
  region STRING DEFAULT 'EU',
  name STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
CLUSTER BY AUTO
COMMENT 'One row per customer'
TBLPROPERTIES (
  'delta.columnMapping.mode' = 'name',
  'delta.feature.allowColumnDefaults' = 'supported',
  'my.example' = 'yes'
)
WITH ROW FILTER roundtrip.visible_region ON (region);

ALTER TABLE roundtrip.customers SET TAGS ('domain' = 'sales', 'reviewed' = '');
ALTER TABLE roundtrip.customers ALTER COLUMN email SET TAGS ('pii' = 'email');
GRANT SELECT ON TABLE roundtrip.customers TO `account users`;
