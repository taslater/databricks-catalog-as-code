CREATE TABLE sc_create_objects.employees (
  employee_id BIGINT NOT NULL,
  manager_id BIGINT,
  CONSTRAINT employees_pk PRIMARY KEY (employee_id)
);

ALTER TABLE sc_create_objects.employees ADD CONSTRAINT employees_manager_fk
  FOREIGN KEY (manager_id) REFERENCES sc_create_objects.employees (employee_id);
