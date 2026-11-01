-- ucstate: transition
-- id: 2026-11-01-narrow-qty
-- object: sc_control_transition_wrong_state.t
-- from: b0b24b1dcd494cdc   sc_control_transition_wrong_state.t as it is in ucstate_test now
-- to:   c4ff38abef50eaf0   sc_control_transition_wrong_state.t as the source declares it
--
-- This script replaces every generated statement for sc_control_transition_wrong_state.t. It runs
-- only while the table is in the `from` state, and the deploy fails unless
-- the table then matches the source. A target without the table never runs
-- it: the table is created as declared.
--
-- What the plan found:
--   rewrite: column qty changes from bigint to int
--
-- The statements below are a starting point. CREATE OR REPLACE ... AS SELECT
-- keeps the table's id, grants and tags, but takes no column list or
-- constraints. Check each CAST: it is where rows can fail or change.

CREATE OR REPLACE TABLE sc_control_transition_wrong_state.t
COMMENT 'Stock'
AS SELECT
  id AS id,
  CAST(qty AS INT) AS qty
FROM sc_control_transition_wrong_state.t;
ALTER TABLE sc_control_transition_wrong_state.t ALTER COLUMN id SET NOT NULL;
ALTER TABLE sc_control_transition_wrong_state.t ALTER COLUMN id COMMENT 'Key';
ALTER TABLE sc_control_transition_wrong_state.t ALTER COLUMN qty COMMENT 'Units';
