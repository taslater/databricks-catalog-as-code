CREATE FUNCTION roundtrip.add_tax(amount DOUBLE, rate DOUBLE DEFAULT 0.2 COMMENT 'Tax rate as a fraction')
RETURNS DOUBLE
DETERMINISTIC
RETURN amount * (1 + rate);
