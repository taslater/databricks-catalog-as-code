CREATE VOLUME sc_metadata.files COMMENT 'After';

ALTER VOLUME sc_metadata.files SET TAGS ('stage' = 'clean');
