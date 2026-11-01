CREATE TABLE sc_constraints.child (
  child_id BIGINT NOT NULL,
  parent_id BIGINT,
  CONSTRAINT child_pk PRIMARY KEY (child_id),
  CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES sc_constraints.parent (id)
);
