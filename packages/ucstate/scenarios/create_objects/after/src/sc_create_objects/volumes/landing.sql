CREATE VOLUME sc_create_objects.landing COMMENT 'Files to load';

ALTER VOLUME sc_create_objects.landing SET TAGS ('stage' = 'raw');
GRANT READ VOLUME ON VOLUME sc_create_objects.landing TO `account users`;
