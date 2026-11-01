CREATE TABLE sc_constraints.links (
  child_id BIGINT,
  CONSTRAINT links_child_fk FOREIGN KEY (child_id) REFERENCES sc_constraints.child (child_id)
);
