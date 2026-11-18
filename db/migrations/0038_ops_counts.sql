-- 0038_ops_counts.sql
-- The console Overview and the coverage block of search results counted whole
-- tables on every load: five ops_* views over communication_versions,
-- classification_labels and email_attachments, and coverage_counts over every
-- communication. With millions of rows each load cost seconds to minutes.
--
-- ops_counts keeps the answers of those six views: one row per view, its rows as
-- a JSON array. Counts and codes only, never an id or any content. Nothing writes
-- it per email. refresh_ops_counts() recounts all six in one transaction. The
-- ingestion app calls it on every scheduler and processing tick (every 5
-- minutes) and after each backfill window; it recounts only when a run has
-- finished since the last recount (ops_runs.finished_at) and that recount is at
-- least min_age (5 minutes) old. An idle call reads the six ops_counts rows and
-- scans ops_runs (one row per capture and per processing run that did work),
-- and counts nothing. The CLI, whose capture and process record no run, passes
-- changed => true after its work.
--
-- The migration does not fill the table. Empty, or without this migration, the
-- code reads the live views as before (ci_core.ops.overview,
-- ci_retrieval.search.coverage).
--
-- coverage_counts depends on the mode (ci.active_dataset()); the ops views do
-- not. The coverage row records the dataset it was counted for, and
-- coverage_snapshot shows it only while that is still the active dataset. After a
-- mode change the next refresh ignores min_age.
--
-- Access: operators read the table (the dashboard). The search role gets
-- coverage_snapshot only, which is the same counts it already reads from
-- coverage_counts; it gets nothing on ops_counts. Ingestion may only call the
-- function; it cannot read or write the table itself.
-- docs/DATABASE.md "Stored counts", docs/DEPLOY_AZURE.md "Search speed".

SET search_path = ci, public;

CREATE TABLE ops_counts (
    name         text PRIMARY KEY CHECK (name IN
                 ('versions', 'failures', 'labels', 'extraction', 'originals', 'coverage')),
    rows         jsonb NOT NULL,          -- the view's rows as a JSON array; counts and codes only
    dataset_key  text,                    -- coverage only: the dataset counted (NULL = paused)
    refreshed_at timestamptz NOT NULL     -- when counting started
);

REVOKE ALL ON ops_counts FROM PUBLIC;

-- True when it recounted. False without counting when another session is
-- recounting right now (it does not wait), or when all six rows exist, the
-- coverage row is for the active dataset, and either they are younger than
-- min_age or nothing changed since: no ops_runs row finished after them and the
-- caller did not pass changed => true. Missing rows or a mode change always
-- recount. min_age is at least 1 minute whatever the caller passes. It waits
-- at most 5 s for a lock (a migration, a long write): then it fails and the
-- old counts stay. Its time
-- limit is set by the caller (ci_core.ops.refresh_counts, 3 minutes): a
-- statement_timeout set on a function does not bound the statement calling it.
CREATE FUNCTION refresh_ops_counts(min_age interval DEFAULT '5 minutes',
                                   changed boolean DEFAULT false)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ci, public, pg_temp
SET lock_timeout = '5s'
AS $$
DECLARE
    started timestamptz := now();   -- transaction start: one value, never later than the data
    dataset text := ci.active_dataset();
    counted_at timestamptz;
    least_age interval := greatest(min_age, interval '1 minute');
BEGIN
    IF NOT pg_try_advisory_xact_lock(hashtext('ci.refresh_ops_counts')) THEN
        RETURN false;
    END IF;
    SELECT CASE WHEN count(*) = 6 THEN min(refreshed_at) END INTO counted_at FROM ops_counts;
    IF counted_at IS NOT NULL
       AND (SELECT dataset_key IS NOT DISTINCT FROM dataset
            FROM ops_counts WHERE name = 'coverage') THEN
        IF counted_at > started - least_age THEN
            RETURN false;   -- young enough
        END IF;
        IF NOT changed
           AND NOT EXISTS (SELECT 1 FROM ops_runs WHERE finished_at > counted_at) THEN
            RETURN false;   -- nothing finished since: the counts are still right
        END IF;
    END IF;
    INSERT INTO ops_counts (name, rows, dataset_key, refreshed_at)
    SELECT 'versions', coalesce(jsonb_agg(to_jsonb(v)), '[]'), NULL, started
      FROM ops_version_states v
    UNION ALL
    SELECT 'failures', coalesce(jsonb_agg(to_jsonb(v)), '[]'), NULL, started
      FROM ops_failures v
    UNION ALL
    SELECT 'labels', coalesce(jsonb_agg(to_jsonb(v)), '[]'), NULL, started
      FROM ops_label_counts v
    UNION ALL
    SELECT 'extraction', coalesce(jsonb_agg(to_jsonb(v)), '[]'), NULL, started
      FROM ops_extraction_counts v
    UNION ALL
    SELECT 'originals', coalesce(jsonb_agg(to_jsonb(v)), '[]'), NULL, started
      FROM ops_original_counts v
    UNION ALL
    SELECT 'coverage', coalesce(jsonb_agg(to_jsonb(v)), '[]'), dataset, started
      FROM coverage_counts v
    ON CONFLICT (name) DO UPDATE
        SET rows = EXCLUDED.rows,
            dataset_key = EXCLUDED.dataset_key,
            refreshed_at = EXCLUDED.refreshed_at;
    RETURN true;
END $$;

-- The stored coverage counts, only while they are for the active dataset.
-- A security_barrier view: the caller's own conditions are applied after its
-- WHERE, to the coverage row only.
CREATE VIEW coverage_snapshot WITH (security_barrier) AS
SELECT rows, refreshed_at
FROM ops_counts
WHERE name = 'coverage' AND dataset_key IS NOT DISTINCT FROM ci.active_dataset();

REVOKE ALL ON FUNCTION refresh_ops_counts(interval, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_ops_counts(interval, boolean) TO ci_ingest;
GRANT SELECT ON ops_counts TO ci_operator;
GRANT SELECT ON coverage_snapshot TO ci_search;
