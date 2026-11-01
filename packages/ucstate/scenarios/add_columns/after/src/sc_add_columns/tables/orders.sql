CREATE TABLE sc_add_columns.orders (
  order_id BIGINT NOT NULL,
  note STRING DEFAULT 'none' COMMENT 'Free text',
  amount DECIMAL(10,2),
  shipped BOOLEAN
)
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');

ALTER TABLE sc_add_columns.orders ALTER COLUMN note SET TAGS ('pii' = 'no');
