CREATE TABLE sc_constraints.people (
  person_id BIGINT NOT NULL,
  manager_id BIGINT,
  CONSTRAINT people_pk PRIMARY KEY (person_id)
);
