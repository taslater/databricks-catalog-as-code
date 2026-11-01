CREATE TABLE sc_metadata.t (
  id BIGINT,
  name STRING COMMENT 'Display name'
)
COMMENT 'After'
TBLPROPERTIES ('my.team' = 'platform', 'delta.logRetentionDuration' = 'interval 60 days');

ALTER TABLE sc_metadata.t SET TAGS ('domain' = 'finance', 'reviewed' = '');
ALTER TABLE sc_metadata.t ALTER COLUMN id SET TAGS ('key' = 'yes');
