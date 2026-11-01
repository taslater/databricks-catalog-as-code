CREATE TABLE sc_metadata.t (
  id BIGINT COMMENT 'Old comment',
  name STRING
)
COMMENT 'Before'
TBLPROPERTIES ('my.team' = 'data', 'delta.logRetentionDuration' = 'interval 30 days');

ALTER TABLE sc_metadata.t SET TAGS ('domain' = 'sales', 'stale' = 'yes');
ALTER TABLE sc_metadata.t ALTER COLUMN name SET TAGS ('pii' = 'name');
