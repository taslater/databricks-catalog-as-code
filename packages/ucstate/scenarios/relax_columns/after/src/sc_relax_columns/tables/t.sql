CREATE TABLE sc_relax_columns.t (
  id BIGINT NOT NULL,
  code STRING,
  status STRING,
  region STRING DEFAULT 'US',
  kind STRING DEFAULT 'plain'
)
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
