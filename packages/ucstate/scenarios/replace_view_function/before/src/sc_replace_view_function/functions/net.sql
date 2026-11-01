CREATE FUNCTION sc_replace_view_function.net(amount DOUBLE) RETURNS DOUBLE RETURN amount * 0.8;

GRANT EXECUTE ON FUNCTION sc_replace_view_function.net TO `account users`;
