CREATE FUNCTION sc_governance.mask_b(v STRING) RETURNS STRING RETURN left(v, 1);

GRANT EXECUTE ON FUNCTION sc_governance.mask_b TO `account users`;
