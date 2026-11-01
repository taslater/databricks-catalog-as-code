CREATE FUNCTION sc_create_objects.eu_only(region STRING)
RETURNS BOOLEAN
RETURN region = 'EU';
