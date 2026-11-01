CREATE SCHEMA roundtrip
COMMENT 'Round-trip coverage for ucstate import';

ALTER SCHEMA roundtrip SET TAGS ('purpose' = 'test');
GRANT USE SCHEMA ON SCHEMA roundtrip TO `account users`;
