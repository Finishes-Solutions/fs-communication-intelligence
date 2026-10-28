-- 0005_ops.sql
-- Operations state for the admin console: the capture/processing schedule (so
-- it can change without redeploying), a history of runs, and count-only views.
-- Content stays behind the published_* views; nothing here exposes message
-- text. Restricted messages (CLAUDE.md section 7) are never shown anywhere.

SET search_path = ci, public;

-- Exactly one row. The ingestion app's timer ticks every 5 minutes and reads it.
CREATE TABLE ops_settings (
    id                     boolean PRIMARY KEY DEFAULT true CHECK (id),
    capture_enabled        boolean NOT NULL DEFAULT false,
    capture_interval_min   integer NOT NULL DEFAULT 60
                           CHECK (capture_interval_min IN (15, 30, 60, 120, 240, 720, 1440)),
    capture_window_hours   numeric(6, 2) NOT NULL DEFAULT 2
                           CHECK (capture_window_hours > 0 AND capture_window_hours <= 744),
    active_start_hour      smallint NOT NULL DEFAULT 0 CHECK (active_start_hour BETWEEN 0 AND 23),
    active_end_hour        smallint NOT NULL DEFAULT 24 CHECK (active_end_hour BETWEEN 1 AND 24),
    weekdays_only          boolean NOT NULL DEFAULT false,
    time_zone              text NOT NULL DEFAULT 'America/Chicago',
    processing_enabled     boolean NOT NULL DEFAULT true,
    run_requested_at       timestamptz,          -- "run now"; cleared when a run starts
    run_requested_hours    numeric(6, 2) CHECK (run_requested_hours IS NULL
                           OR (run_requested_hours > 0 AND run_requested_hours <= 744)),
    last_capture_started_at timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text NOT NULL DEFAULT 'migration',
    CHECK (active_end_hour > active_start_hour)
);
INSERT INTO ops_settings (id) VALUES (true);

CREATE TABLE ops_runs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('capture', 'process')),
    trigger      text NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    window_hours numeric(6, 2),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'refused')),
    counts       jsonb NOT NULL DEFAULT '{}',    -- counts and codes only
    error_code   text
);
CREATE INDEX ops_runs_recent ON ops_runs (started_at DESC);

-- Count-only views for the dashboard.
CREATE VIEW ops_version_states AS
SELECT v.processing_state, coalesce(split_part(v.hold_reason_code, ':', 1), '') AS outcome,
       coalesce(v.hold_reason_code, '') AS reason, count(*) AS versions
FROM communication_versions v
GROUP BY 1, 2, 3;

CREATE VIEW ops_backlog AS
SELECT count(*) AS fetched, min(created_at) AS oldest_fetched_at
FROM communication_versions WHERE processing_state = 'fetched';

CREATE VIEW ops_failures AS
SELECT coalesce(last_error_code, 'unknown') AS error_code, count(*) AS versions
FROM communication_versions WHERE processing_state = 'failed' GROUP BY 1;

CREATE VIEW ops_label_counts AS
SELECT l.label, l.decision, count(*) AS messages
FROM classification_labels l GROUP BY 1, 2;

CREATE VIEW ops_extraction_counts AS
SELECT extraction_status, count(*) AS attachments FROM email_attachments GROUP BY 1;

CREATE VIEW ops_mailbox_states AS
SELECT s.scope_state, s.test_approved, s.production_approved, count(*) AS mailboxes
FROM source_scopes s WHERE s.scope_kind = 'mailbox' GROUP BY 1, 2, 3;

-- Messages that were not published: who, where, when and why, never the text
-- or subject (restricted mail must never be shown).
CREATE VIEW ops_unpublished AS
SELECT c.id AS communication_id, v.id AS version_id, v.processing_state,
       v.hold_reason_code, v.last_error_code, c.occurred_at,
       m.primary_smtp_address AS mailbox,
       (SELECT p.email_normalized FROM communication_participants cp
          JOIN participants p ON p.id = cp.participant_id
         WHERE cp.communication_id = c.id AND cp.role = 'from' LIMIT 1) AS sender
FROM communication_versions v
JOIN communications c ON c.id = v.communication_id
JOIN email_messages em ON em.communication_id = c.id
JOIN email_mailboxes m ON m.scope_id = em.mailbox_scope_id
WHERE v.processing_state IN ('held', 'rejected', 'failed', 'fetched');

-- Operators change the schedule; ingestion may only record that a run started.
GRANT SELECT, UPDATE ON ops_settings TO ci_operator;
GRANT SELECT ON ops_settings TO ci_ingest;
GRANT UPDATE (run_requested_at, run_requested_hours, last_capture_started_at)
    ON ops_settings TO ci_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops_runs TO ci_ingest;
GRANT SELECT ON ops_runs, ops_version_states, ops_backlog, ops_failures, ops_label_counts,
    ops_extraction_counts, ops_mailbox_states, ops_unpublished TO ci_operator;
-- Operators can requeue failed versions (state only, never content).
GRANT SELECT, UPDATE (processing_state, last_error_code) ON communication_versions TO ci_operator;
