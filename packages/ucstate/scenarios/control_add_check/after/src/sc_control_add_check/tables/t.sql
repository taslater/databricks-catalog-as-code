CREATE TABLE sc_control_add_check.t (id BIGINT, amount DECIMAL(10,2));

ALTER TABLE sc_control_add_check.t ADD CONSTRAINT amount_positive CHECK (amount > 0);
