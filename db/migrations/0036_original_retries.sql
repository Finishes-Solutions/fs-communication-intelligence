-- 0036_original_retries.sql
-- The original-file backfill (ci_email.process.store_missing_originals) retried
-- every file it could not download on every 5-minute processing run, forever:
-- the run history showed "original_download_failed 27" on all 462 runs of
-- 2026-11-17/18 (Matthew asked why). A file whose message was deleted or moved
-- out of its mailbox after capture can never be downloaded again.
-- Now each failure is recorded: a permanent one (message not found, mailbox out
-- of reach) stops the retries at once; any other is retried after 2, 4, 8 and
-- 16 hours and then left alone. The email itself is unaffected (it stays
-- searchable); only its original file is missing from the console.
-- Adding columns with a constant default does not rewrite the table.

SET search_path = ci, public;

ALTER TABLE email_attachments
    ADD COLUMN IF NOT EXISTS original_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS original_failed_at timestamptz,
    ADD COLUMN IF NOT EXISTS original_fail_reason text
        CHECK (original_fail_reason IS NULL OR original_fail_reason ~ '^[A-Za-z][A-Za-z0-9_]{0,63}$');
