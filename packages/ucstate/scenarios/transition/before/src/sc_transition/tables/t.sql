CREATE TABLE sc_transition.t (
  id BIGINT NOT NULL COMMENT 'Key',
  qty BIGINT COMMENT 'Units',
  CONSTRAINT t_pk PRIMARY KEY (id)
)
COMMENT 'Stock';
