CREATE FUNCTION sc_replace_view_function.mask_status(v STRING) RETURNS STRING RETURN left(v, 1);

GRANT EXECUTE ON FUNCTION sc_replace_view_function.mask_status TO `account users`;
