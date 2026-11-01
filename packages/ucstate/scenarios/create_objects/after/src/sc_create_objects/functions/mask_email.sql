CREATE FUNCTION sc_create_objects.mask_email(email STRING)
RETURNS STRING
RETURN concat('***@', substring_index(email, '@', -1));
