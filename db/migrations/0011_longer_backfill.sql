-- 0011_longer_backfill.sql
-- A requested ("run now") capture may look back up to 93 days (2232 hours), so a
-- backfill from 2026-09-01 fits in one request (Matthew, 2026-10-28). The
-- scheduled look-back (capture_window_hours) keeps its 744-hour limit.

SET search_path = ci, public;

ALTER TABLE ops_settings DROP CONSTRAINT ops_settings_run_requested_hours_check;
ALTER TABLE ops_settings ADD CONSTRAINT ops_settings_run_requested_hours_check
    CHECK (run_requested_hours IS NULL
           OR (run_requested_hours > 0 AND run_requested_hours <= 2232));
