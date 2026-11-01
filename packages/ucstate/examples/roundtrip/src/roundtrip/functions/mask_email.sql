CREATE FUNCTION roundtrip.mask_email(email STRING)
RETURNS STRING
DETERMINISTIC
COMMENT 'Keeps only the domain'
RETURN concat('***@', substring_index(email, '@', -1));
