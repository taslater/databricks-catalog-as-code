CREATE FUNCTION sc_create_objects.big_orders(threshold DECIMAL(10,2))
RETURNS TABLE (order_id BIGINT, amount DECIMAL(10,2))
READS SQL DATA
RETURN SELECT order_id, amount FROM sc_create_objects.orders WHERE amount > threshold;

GRANT EXECUTE ON FUNCTION sc_create_objects.big_orders TO `account users`;
