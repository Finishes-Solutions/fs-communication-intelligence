-- 0027_meetings.sql
-- Meetings from Fireflies (docs/MEETINGS_AND_CALLS_PLAN.md, M1): the Fireflies team
-- as capture scopes, meeting metadata beside the source-neutral communications,
-- transcript and vendor-summary texts, and backfill windows that name their source.
-- Decided by Matthew on 2026-11-04 (D-M1: full backfill from 2025-01-01; D-M2: the
-- automation account's API key). Transcript text is content and never leaves the
-- version_texts / transcript_segments rows for logs.

SET search_path = ci, public;
-- Safe beside a running mail backfill: give up after 5 s instead of queueing behind a
-- long transaction (which would block every new capture and processing write while
-- it waits). If it gives up, nothing is applied; run `make azure-migrate` again.
SET LOCAL lock_timeout = '5s';

-- One row per Fireflies team member (source_scopes.scope_kind = 'fireflies_user'),
-- written by inventory sync. native_scope_id on the scope is the Fireflies user id.
CREATE TABLE fireflies_users (
    scope_id          uuid PRIMARY KEY REFERENCES source_scopes(id) ON DELETE CASCADE,
    email             text NOT NULL CHECK (email = lower(email)),
    is_admin          boolean NOT NULL DEFAULT false,
    last_inventory_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX fireflies_users_email ON fireflies_users (email);

-- Content-free metadata for one meeting (communications.kind = 'meeting',
-- provider 'fireflies'; native_id = the Fireflies transcript id).
CREATE TABLE fireflies_meetings (
    communication_id uuid PRIMARY KEY REFERENCES communications(id) ON DELETE CASCADE,
    organizer_email  text,
    host_email       text,
    meeting_link     text,              -- Teams/Zoom join link when Fireflies knew one
    transcript_url   text,              -- app.fireflies.ai/view/<id>
    duration_minutes numeric(8, 2),
    sentence_count   integer NOT NULL DEFAULT 0,
    speaker_count    integer NOT NULL DEFAULT 0,
    attendee_count   integer NOT NULL DEFAULT 0,
    summary_status   text,
    has_summary      boolean NOT NULL DEFAULT false,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fireflies_meetings_organizer ON fireflies_meetings (organizer_email);
CREATE INDEX fireflies_meetings_link ON fireflies_meetings (meeting_link)
    WHERE meeting_link IS NOT NULL;

-- Transcript text (one "[mm:ss] Speaker: text" line per sentence) and the vendor's
-- own summary (AI output, labelled as such) as version texts, so M2 can process
-- meetings through the same text path as email.
-- NOT VALID: no scan of the (large) table under the exclusive lock while a mail
-- backfill is writing to it; 0028 validates it afterwards without blocking writes.
ALTER TABLE version_texts DROP CONSTRAINT version_texts_text_kind_check;
ALTER TABLE version_texts ADD CONSTRAINT version_texts_text_kind_check
    CHECK (text_kind IN ('email_body', 'attachment', 'transcript', 'provider_summary')) NOT VALID;

ALTER TABLE ops_runs DROP CONSTRAINT ops_runs_kind_check;
ALTER TABLE ops_runs ADD CONSTRAINT ops_runs_kind_check
    CHECK (kind IN ('capture', 'process', 'fireflies_capture'));

-- A backfill plan names its source; the scheduler runs each window with that
-- source's capture. One unfinished plan per source at a time.
ALTER TABLE ops_backfill_windows ADD COLUMN source text NOT NULL DEFAULT 'email'
    CHECK (source IN ('email', 'fireflies', 'teams'));
CREATE INDEX ops_backfill_windows_source ON ops_backfill_windows (source, until DESC)
    WHERE state = 'pending';

-- Rolling Fireflies capture (the last FIREFLIES_CAPTURE_DAYS, at most once per
-- FIREFLIES_CAPTURE_INTERVAL_MIN) records when it last started.
ALTER TABLE ops_settings ADD COLUMN last_fireflies_capture_at timestamptz;

-- Citation fields for search, on published_communications so every
-- authorization and exclusion rule from 0001 applies.
CREATE VIEW published_meetings AS
SELECT pc.id AS communication_id, pc.corpus_id, pc.title, pc.occurred_at, pc.duration_seconds,
       pc.source_url, fm.organizer_email, fm.meeting_link, fm.transcript_url,
       fm.sentence_count, fm.speaker_count, fm.attendee_count, fm.has_summary
FROM published_communications pc
JOIN fireflies_meetings fm ON fm.communication_id = pc.id;

GRANT SELECT, INSERT, UPDATE, DELETE ON fireflies_users, fireflies_meetings TO ci_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON transcript_segments TO ci_ingest;
GRANT UPDATE (last_fireflies_capture_at) ON ops_settings TO ci_ingest;
GRANT SELECT ON published_meetings TO ci_search;
-- `ci backfill release`: an operator closes runs a restart killed (state only, never content).
GRANT UPDATE (status, finished_at, error_code) ON ops_runs TO ci_operator;
-- Operators list the team for `ci fireflies scope list`; addresses are not evidence content.
GRANT SELECT ON fireflies_users, fireflies_meetings TO ci_operator;
