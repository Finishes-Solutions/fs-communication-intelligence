-- 0012_fireflies.sql
-- Fireflies meetings, the second source (Matthew, 2026-10-28: "pull Fireflies
-- meetings from the whole company and have AI analysis done, no scanning for
-- PII"). Meetings reuse the source-neutral foundation from 0001: a meeting is a
-- communication of kind 'meeting', its sentences are transcript_segments, and
-- its searchable units are 'transcript_window' and 'provider_summary' chunks.
--
-- Scope: one 'fireflies_workspace' scope stands for the whole Fireflies team.
-- People are kept out with participant exclusions (the published views already
-- apply them by email address) and single meetings with item exclusions.
--
-- No PII screening or masking runs on meetings. Processing records that as a
-- policy_decisions row (check 'pii', decision 'allow', reason
-- 'not_screened_by_policy') so the gap is visible in the data, not only in docs.

SET search_path = ci, public;

ALTER TABLE source_scopes DROP CONSTRAINT source_scopes_scope_kind_check;
ALTER TABLE source_scopes ADD CONSTRAINT source_scopes_scope_kind_check
    CHECK (scope_kind IN ('mailbox', 'teams_chat', 'teams_channel', 'fireflies_user',
                          'fireflies_workspace', 'dialpad_user'));

-- The Fireflies summary is stored as text next to the transcript segments.
ALTER TABLE version_texts DROP CONSTRAINT version_texts_text_kind_check;
ALTER TABLE version_texts ADD CONSTRAINT version_texts_text_kind_check
    CHECK (text_kind IN ('email_body', 'attachment', 'provider_summary'));

-- Content-free meeting metadata (one row per meeting communication).
CREATE TABLE fireflies_meetings (
    communication_id uuid PRIMARY KEY REFERENCES communications(id) ON DELETE CASCADE,
    organizer_email  text,
    host_email       text,
    meeting_link     text,
    sentence_count   integer NOT NULL DEFAULT 0,
    has_provider_summary boolean NOT NULL DEFAULT false,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- AI analysis of one meeting version. A proposal, never an approval:
-- summary, decisions, action items (owner as said in the meeting; owner_email
-- only when it matches an attendee), project risks, and the property/entity
-- names spoken (canonical when the known_names registry matches).
CREATE TABLE meeting_analyses (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id     uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    model          text NOT NULL,
    policy_version text NOT NULL,
    input_sha256   text NOT NULL,
    summary        text,
    decisions      text[] NOT NULL DEFAULT '{}',
    action_items   jsonb NOT NULL DEFAULT '[]',  -- [{owner, owner_email, task, due}]
    risks          text[] NOT NULL DEFAULT '{}',
    properties     text[] NOT NULL DEFAULT '{}',
    entities       text[] NOT NULL DEFAULT '{}',
    probabilities  jsonb NOT NULL DEFAULT '{}',  -- label -> probability or null
    yes_labels     text[] NOT NULL DEFAULT '{}',
    uncertain_labels text[] NOT NULL DEFAULT '{}',
    input_truncated boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (version_id, model, policy_version)
);
CREATE INDEX meeting_analyses_input ON meeting_analyses (input_sha256, model, policy_version);

-- Schedule for the Fireflies timers in the ingestion app (one row). Separate
-- from the email schedule so either source can be turned off on its own.
CREATE TABLE fireflies_settings (
    id                      boolean PRIMARY KEY DEFAULT true CHECK (id),
    capture_enabled         boolean NOT NULL DEFAULT false,
    capture_interval_min    integer NOT NULL DEFAULT 60
                            CHECK (capture_interval_min IN (15, 30, 60, 120, 240, 720, 1440)),
    -- Transcripts appear some time after a meeting ends, so look back further than the interval.
    lookback_hours          numeric(6, 2) NOT NULL DEFAULT 72
                            CHECK (lookback_hours > 0 AND lookback_hours <= 744),
    processing_enabled      boolean NOT NULL DEFAULT true,
    run_requested_at        timestamptz,
    run_requested_hours     numeric(6, 2) CHECK (run_requested_hours IS NULL
                            OR (run_requested_hours > 0 AND run_requested_hours <= 2232)),
    last_capture_started_at timestamptz,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by              text NOT NULL DEFAULT 'migration'
);
INSERT INTO fireflies_settings (id) VALUES (true);

ALTER TABLE ops_runs DROP CONSTRAINT ops_runs_kind_check;
ALTER TABLE ops_runs ADD CONSTRAINT ops_runs_kind_check
    CHECK (kind IN ('capture', 'process', 'fireflies_capture', 'fireflies_process'));

-- Search coverage stays about email (meetings get their own counts below).
CREATE OR REPLACE VIEW coverage_counts AS
SELECT v.processing_state, coalesce(v.hold_reason_code, '') AS reason, count(*) AS messages
FROM communications c
JOIN LATERAL (SELECT processing_state, hold_reason_code FROM ci.communication_versions v
              WHERE v.communication_id = c.id ORDER BY v.version_no DESC LIMIT 1) v ON true
WHERE c.dataset_key = ci.active_dataset() AND c.kind = 'email'
GROUP BY v.processing_state, coalesce(v.hold_reason_code, '');

-- Read side for a later meeting search/MCP tool. Built on
-- published_communications, so mode, scope, item and participant exclusions apply.
CREATE VIEW published_meetings AS
SELECT pc.id AS communication_id, pc.corpus_id, pc.title, pc.occurred_at, pc.ended_at,
       pc.duration_seconds, pc.source_url, m.organizer_email, m.host_email,
       m.sentence_count, m.has_provider_summary
FROM published_communications pc
JOIN fireflies_meetings m ON m.communication_id = pc.id;

CREATE VIEW published_meeting_analyses AS
SELECT DISTINCT ON (pc.id)
       pc.id AS communication_id, pc.corpus_id, a.summary, a.decisions, a.action_items,
       a.risks, a.properties, a.entities, a.yes_labels, a.uncertain_labels, a.model,
       a.policy_version, a.input_truncated
FROM published_communications pc
JOIN meeting_analyses a ON a.version_id = pc.active_version_id
ORDER BY pc.id, a.id DESC;

-- Count-only view for operators (no titles, no text).
CREATE VIEW ops_meeting_states AS
SELECT v.processing_state, coalesce(v.hold_reason_code, '') AS reason, count(*) AS meetings
FROM communication_versions v
JOIN fireflies_meetings m ON m.communication_id = v.communication_id
GROUP BY 1, 2;

GRANT SELECT, INSERT, UPDATE, DELETE ON fireflies_meetings, meeting_analyses TO ci_ingest;
GRANT SELECT ON fireflies_settings TO ci_ingest;
GRANT UPDATE (run_requested_at, run_requested_hours, last_capture_started_at)
    ON fireflies_settings TO ci_ingest;
GRANT SELECT, UPDATE ON fireflies_settings TO ci_operator;
GRANT SELECT ON ops_meeting_states TO ci_operator;
GRANT SELECT ON published_meetings, published_meeting_analyses TO ci_search;
