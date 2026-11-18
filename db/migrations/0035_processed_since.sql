-- 0035_processed_since.sql
-- Pollers ask for emails by when they were processed, not when they arrived
-- (Matthew, 2026-11-18, for the AMP relay: "an email that finishes processing a
-- few hours after it arrived can be missed by a poller asking 'since my last
-- check'"). list_email(processed_since=..., cursor=...) walks published versions
-- in published_at order; this partial index lets it start at the cursor and stop
-- after one page instead of sorting every published email.
-- A small table (one row per published version); on the dev server it can be
-- built first by hand with CREATE INDEX CONCURRENTLY under the same name, which
-- makes this a no-op.

SET search_path = ci, public;

CREATE INDEX IF NOT EXISTS communication_versions_published_order
    ON communication_versions (published_at, communication_id)
    WHERE processing_state = 'published';

-- Published emails in the order they were processed, after a cursor (published_at,
-- communication_id). Like the 0031 functions it returns published emails only and
-- the caller applies the reader's grants and limits (ci_retrieval.search._AUTH) to
-- every row before anything is shown; it only orders and pages.
CREATE OR REPLACE FUNCTION published_processed(after_at timestamptz, after_id uuid, n integer)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               published_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    SELECT pc.id, pc.corpus_id, pc.occurred_at, v.published_at
    FROM communication_versions v
    JOIN published_communications pc ON pc.active_version_id = v.id
    WHERE v.processing_state = 'published'
      AND (v.published_at, v.communication_id) > (after_at, after_id)
    ORDER BY v.published_at, v.communication_id
    LIMIT least(greatest(n, 1), 2000)
$$;

REVOKE ALL ON FUNCTION published_processed(timestamptz, uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_processed(timestamptz, uuid, integer) TO ci_search;
