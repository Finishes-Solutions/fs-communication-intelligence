-- 0035_policy_decisions_version.sql
-- policy_decisions had no index on version_id. Processing asks it about one
-- version for every email: the pre-filter's "did an operator ask for this one in
-- full?" (ci_email.process._prefilter), the requeue of skipped and re-screened
-- mail, the delete before a version is processed again, and the ON DELETE
-- CASCADE from communication_versions. Each of those read the whole table. On
-- the dev server (2026-11-18): 2.2 million rows and 1.3 million sequential scans.
--
-- A plain CREATE INDEX, because the runner applies each file in one transaction
-- and CREATE INDEX CONCURRENTLY cannot run in one. It takes a SHARE lock: reads
-- continue, writes to policy_decisions (processing) wait until it is built. The
-- lock_timeout makes the migration fail cleanly, changing nothing, instead of
-- queueing behind a long processing transaction and stalling ingestion behind it.
-- On a server with mail being processed, either turn processing off first, or
-- build the index by hand with CREATE INDEX CONCURRENTLY under the same name
-- before applying (then this is a no-op, as with 0027). docs/DEPLOY_AZURE.md
-- "Processing speed".

SET search_path = ci, public;
SET LOCAL lock_timeout = '30s';

CREATE INDEX IF NOT EXISTS policy_decisions_version
    ON policy_decisions (version_id, check_name);

ANALYZE policy_decisions;
