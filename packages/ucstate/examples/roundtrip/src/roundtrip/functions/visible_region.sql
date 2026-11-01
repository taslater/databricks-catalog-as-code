CREATE FUNCTION roundtrip.visible_region(region STRING)
RETURNS BOOLEAN
DETERMINISTIC
RETURN region <> 'hidden';
