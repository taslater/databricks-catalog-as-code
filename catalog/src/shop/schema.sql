CREATE SCHEMA shop
COMMENT 'A small synthetic shop: customers, products and their orders';

ALTER SCHEMA shop SET TAGS ('domain' = 'sales');
GRANT USE SCHEMA ON SCHEMA shop TO `account users`;
