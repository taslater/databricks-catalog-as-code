CREATE TABLE sc_rename_table.parent (
  id BIGINT NOT NULL,
  CONSTRAINT parent_pk PRIMARY KEY (id)
)
COMMENT 'Accounts';

GRANT SELECT ON TABLE sc_rename_table.parent TO `account users`;
