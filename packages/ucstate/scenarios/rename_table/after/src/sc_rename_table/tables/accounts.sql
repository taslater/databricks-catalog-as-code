CREATE TABLE sc_rename_table.accounts (
  id BIGINT NOT NULL,
  CONSTRAINT parent_pk PRIMARY KEY (id)
)
COMMENT 'Accounts';

GRANT SELECT ON TABLE sc_rename_table.accounts TO `account users`;
