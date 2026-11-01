CREATE FUNCTION roundtrip.slugify(s STRING)
RETURNS STRING
LANGUAGE PYTHON
NOT DETERMINISTIC
AS $$
return s.lower().replace(' ', '-')
$$;
