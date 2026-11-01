CREATE VOLUME roundtrip.landing
COMMENT 'Raw files';

ALTER VOLUME roundtrip.landing SET TAGS ('zone' = 'raw');
GRANT READ VOLUME ON VOLUME roundtrip.landing TO `account users`;
