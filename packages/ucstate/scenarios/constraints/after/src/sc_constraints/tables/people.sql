CREATE TABLE sc_constraints.people (
  person_id BIGINT NOT NULL,
  manager_id BIGINT,
  CONSTRAINT people_pk PRIMARY KEY (person_id)
);

ALTER TABLE sc_constraints.people ADD CONSTRAINT people_manager_fk
  FOREIGN KEY (manager_id) REFERENCES sc_constraints.people (person_id);
