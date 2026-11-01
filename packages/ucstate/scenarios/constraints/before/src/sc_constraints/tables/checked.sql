CREATE TABLE sc_constraints.checked (x INT NOT NULL);

ALTER TABLE sc_constraints.checked ADD CONSTRAINT x_positive CHECK (x > 0);
