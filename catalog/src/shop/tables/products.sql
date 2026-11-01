CREATE TABLE shop.products (
  product_id BIGINT NOT NULL COMMENT 'Surrogate key',
  sku STRING NOT NULL COMMENT 'Stock-keeping unit',
  name STRING NOT NULL COMMENT 'Display name',
  unit_price DECIMAL(10,2) COMMENT 'List price, in the shop currency',
  CONSTRAINT products_pk PRIMARY KEY (product_id)
)
COMMENT 'One row per product';

GRANT SELECT ON TABLE shop.products TO `account users`;
