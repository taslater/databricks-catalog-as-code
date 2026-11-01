CREATE TABLE sc_add_not_null_default.t (
  id BIGINT,
  status STRING NOT NULL DEFAULT 'new' COMMENT 'Lifecycle'
)
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
