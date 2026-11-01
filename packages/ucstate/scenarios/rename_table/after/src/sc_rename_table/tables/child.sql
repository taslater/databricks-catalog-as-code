CREATE TABLE sc_rename_table.child (
  id BIGINT,
  parent_id BIGINT,
  CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES sc_rename_table.accounts (id)
);
