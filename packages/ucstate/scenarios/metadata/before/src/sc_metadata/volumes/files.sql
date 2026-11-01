CREATE VOLUME sc_metadata.files COMMENT 'Before';

ALTER VOLUME sc_metadata.files SET TAGS ('stage' = 'raw');
