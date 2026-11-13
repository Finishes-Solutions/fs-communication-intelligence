-- 0027_claim_index.sql
-- The processing claim (ci_email.process._claim) takes the oldest 'fetched'
-- version with FOR UPDATE SKIP LOCKED and keeps the row locked while the email
-- is processed. Without an index it sorted every fetched version on each claim,
-- and with hundreds of workers and a six-figure backlog (the 2026-11-03
-- backfill) the claims alone saturated the database. This partial index lets
-- the claim walk the queue in order and stop at the first unlocked row.
-- Applied by hand on the dev server first with CREATE INDEX CONCURRENTLY under
-- the same name, so this is a no-op there.

SET search_path = ci, public;

CREATE INDEX IF NOT EXISTS communication_versions_fetched_queue
    ON communication_versions (created_at)
    WHERE processing_state = 'fetched';
