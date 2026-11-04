-- 0028_email_date_range.sql
-- The received date of the oldest and the newest stored email, for the admin
-- console's Overview (Matthew, 2026-11-04: "a latest email and oldest email
-- widget showing the date and time"). Two timestamps, no content. Reads the
-- communications_occurred index (dataset_key, occurred_at DESC), so it is two
-- index lookups however many emails are stored. occurred_at is Graph's
-- receivedDateTime (ci_email.capture). Counts every captured mailbox copy,
-- including mail not yet processed or not indexed.

SET search_path = ci, public;

CREATE OR REPLACE VIEW ops_email_range AS
SELECT (SELECT min(occurred_at) FROM communications
         WHERE dataset_key = active_dataset() AND kind = 'email') AS oldest_at,
       (SELECT max(occurred_at) FROM communications
         WHERE dataset_key = active_dataset() AND kind = 'email') AS latest_at;

GRANT SELECT ON ops_email_range TO ci_operator;
