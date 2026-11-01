-- ucstate: transition
-- id: 2026-11-01-narrow-qty
-- object: sc_transition.t
-- from: 855f3f84d82dc2a3   sc_transition.t as it is in ucstate_test now
-- to:   517bbc1e8d1f31d1   sc_transition.t as the source declares it
--
-- This script replaces every generated statement for sc_transition.t. It runs
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

CREATE OR REPLACE TABLE sc_transition.t
COMMENT 'Stock'
AS SELECT
  id AS id,
  CAST(qty AS INT) AS qty
FROM sc_transition.t;
ALTER TABLE sc_transition.t ALTER COLUMN id SET NOT NULL;
ALTER TABLE sc_transition.t ALTER COLUMN id COMMENT 'Key';
ALTER TABLE sc_transition.t ALTER COLUMN qty COMMENT 'Units';
ALTER TABLE sc_transition.t ADD CONSTRAINT t_pk PRIMARY KEY (id);
