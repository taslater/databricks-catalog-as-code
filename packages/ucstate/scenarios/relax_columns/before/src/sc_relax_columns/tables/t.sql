CREATE TABLE sc_relax_columns.t (
  id BIGINT NOT NULL,
  code STRING NOT NULL,
  status STRING DEFAULT 'new',
  region STRING DEFAULT 'EU',
  kind STRING
)
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
