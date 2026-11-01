CREATE FUNCTION roundtrip.big_orders(threshold DECIMAL(10,2))
RETURNS TABLE (order_id bigint, amount decimal(10,2))
DETERMINISTIC
READS SQL DATA
RETURN SELECT order_id, amount FROM roundtrip.orders WHERE amount > threshold;
