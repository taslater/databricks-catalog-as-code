CREATE TABLE sc_rename_column.t (
  id BIGINT NOT NULL,
  full_name STRING COMMENT 'Full name',
  CONSTRAINT t_pk PRIMARY KEY (id)
)
TBLPROPERTIES ('delta.columnMapping.mode' = 'name');
