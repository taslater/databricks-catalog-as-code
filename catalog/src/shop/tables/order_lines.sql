CREATE TABLE shop.order_lines (
  order_id BIGINT NOT NULL COMMENT 'The order this line is on',
  line_no INT NOT NULL COMMENT 'Position on the order, from 1',
  product_id BIGINT NOT NULL COMMENT 'What was ordered',
  quantity INT NOT NULL COMMENT 'How many',
  unit_price DECIMAL(10,2) COMMENT 'Price paid per unit',
  CONSTRAINT order_lines_pk PRIMARY KEY (order_id, line_no),
  CONSTRAINT order_lines_order_fk FOREIGN KEY (order_id) REFERENCES shop.orders (order_id),
  CONSTRAINT order_lines_product_fk FOREIGN KEY (product_id) REFERENCES shop.products (product_id)
)
COMMENT 'One row per product on an order';
