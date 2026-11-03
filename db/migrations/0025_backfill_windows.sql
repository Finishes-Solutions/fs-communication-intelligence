-- 0025_backfill_windows.sql
-- A backfill plan: the period to capture, cut into windows the ingestion app
-- works through one at a time, newest first (Matthew, 2026-11-03: all mail back
-- to 2025-01-01). Each window is one capture run (ops_runs, trigger 'backfill'),
-- so a stopped host resumes at the next pending window and emails already
-- stored are skipped. A single requested capture keeps its 93-day limit.

SET search_path = ci, public;

CREATE TABLE ops_backfill_windows (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id      uuid NOT NULL,                 -- one `ci backfill plan` call
    since        timestamptz NOT NULL,
    until        timestamptz NOT NULL,
    state        text NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    run_id       bigint REFERENCES ops_runs (id) ON DELETE SET NULL,
    started_at   timestamptz,
    finished_at  timestamptz,
    counts       jsonb NOT NULL DEFAULT '{}',   -- counts and codes only
    error_code   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    CHECK (until > since),
    CHECK (until - since <= interval '93 days')
);
-- The next window to run: newest first, so recent history is searchable soonest.
CREATE INDEX ops_backfill_windows_next ON ops_backfill_windows (until DESC) WHERE state = 'pending';
CREATE INDEX ops_backfill_windows_plan ON ops_backfill_windows (plan_id);

ALTER TABLE ops_runs DROP CONSTRAINT ops_runs_trigger_check;
ALTER TABLE ops_runs ADD CONSTRAINT ops_runs_trigger_check
    CHECK (trigger IN ('schedule', 'manual', 'backfill'));

-- Operators plan and cancel; the ingestion app claims and finishes windows.
GRANT SELECT, INSERT, UPDATE, DELETE ON ops_backfill_windows TO ci_operator;
GRANT SELECT, UPDATE (state, run_id, started_at, finished_at, counts, error_code)
    ON ops_backfill_windows TO ci_ingest;
