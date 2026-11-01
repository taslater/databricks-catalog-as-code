-- ucstate: once
-- id: 2026-11-01-statuses
-- phase: post
UPDATE sc_once_script.t SET status = 'new' WHERE status = 'old';
