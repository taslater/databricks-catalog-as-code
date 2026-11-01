CREATE TABLE sc_add_columns.orders (
  order_id BIGINT NOT NULL,
  amount DECIMAL(10,2)
)
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
