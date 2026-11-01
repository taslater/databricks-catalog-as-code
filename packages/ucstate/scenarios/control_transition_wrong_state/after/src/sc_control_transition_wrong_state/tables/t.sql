CREATE TABLE sc_control_transition_wrong_state.t (
  id BIGINT NOT NULL COMMENT 'Key',
  qty INT COMMENT 'Units',
  CONSTRAINT t_pk PRIMARY KEY (id)
)
COMMENT 'Stock';
