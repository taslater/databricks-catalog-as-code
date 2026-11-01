CREATE TABLE sc_first_default.t (id BIGINT, status STRING DEFAULT 'new')
TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported');
