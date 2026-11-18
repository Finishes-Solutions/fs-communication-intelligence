-- 0036_ops_counts.sql
-- The console Overview and the coverage block of search results counted whole
-- tables on every load: five ops_* views over communication_versions,
-- classification_labels and email_attachments, and coverage_counts over every
-- communication. With millions of rows each load cost seconds to minutes.
--
-- ops_counts keeps the answers of those six views: one row per view, its rows as
-- a JSON array. Counts and codes only, never an id or any content. Nothing writes
-- it per email. refresh_ops_counts() recounts all six in one transaction, and the
-- ingestion app calls it after a capture or processing run that did work, at most
-- once per min_age (5 minutes).
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

-- True when it recounted. False when another session is recounting right now
-- (it does not wait), or when all six rows are younger than min_age and the
-- coverage row is for the active dataset.
CREATE FUNCTION refresh_ops_counts(min_age interval DEFAULT '5 minutes')
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ci, public
AS $$
DECLARE
    started timestamptz := now();   -- transaction start: one value, never later than the data
    dataset text := ci.active_dataset();
BEGIN
    IF NOT pg_try_advisory_xact_lock(hashtext('ci.refresh_ops_counts')) THEN
        RETURN false;
    END IF;
    IF (SELECT count(*) = 6 AND min(refreshed_at) > started - min_age FROM ops_counts)
       AND (SELECT dataset_key IS NOT DISTINCT FROM dataset
            FROM ops_counts WHERE name = 'coverage') THEN
        RETURN false;
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
CREATE VIEW coverage_snapshot AS
SELECT rows, refreshed_at
FROM ops_counts
WHERE name = 'coverage' AND dataset_key IS NOT DISTINCT FROM ci.active_dataset();

REVOKE ALL ON FUNCTION refresh_ops_counts(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_ops_counts(interval) TO ci_ingest;
GRANT SELECT ON ops_counts TO ci_operator;
GRANT SELECT ON coverage_snapshot TO ci_search;
