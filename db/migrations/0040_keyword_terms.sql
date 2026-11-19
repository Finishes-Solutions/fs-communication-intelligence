-- 0040_keyword_terms.sql
-- The keyword half of search, read from a small table instead of the chunks table.
--
-- Measured on the dev server (2026-11-18, E16ds_v5, 4.95 million chunks): finding
-- the chunks that contain "retainage" took 1.1 s (16,016 matches) and "change
-- order" 0.1 s (86,763), but ranking them (ts_rank_cd, to keep the best 400) did
-- not finish in 120 s. Ranking reads each match's tsvector, and chunks.tsv is kept
-- out of line in the table's 39 GB TOAST storage, between the passage text and the
-- embeddings: one scattered read per match, about 2 ms each while it is not in
-- memory. A bigger server does not change that.
--
-- chunk_terms holds each chunk's tsvector inline (STORAGE MAIN with the largest
-- toast_tuple_target, so it stays in the row, compressed), with the few columns the
-- keyword functions need. About 6 GB of tsvectors plus its GIN index: small enough
-- to stay in memory. published_lexical and published_lexical_in rank from it once
-- chunk_terms_state.ready is true, and as before (0031) until then. The answers are
-- the same either way, except for the limit below (tests/db/test_keyword_terms.py).
--
-- A term that matches more chunks than chunk_terms_state.score_limit (default
-- 20,000) is ranked among its newest score_limit matching chunks only, by the
-- email's date, so a very common word costs a bounded amount. Older matches can
-- still come from the meaning half or a narrower period. Change it with
-- UPDATE ci.chunk_terms_state SET score_limit = N.
--
-- New chunks are added by a trigger. Chunks that exist when this is applied are
-- copied by `python scripts/keyword_table.py build`, which then builds the GIN index
-- without blocking writes and sets ready (docs/DEPLOY_AZURE.md, "Search speed"). On
-- a database with no chunks yet (a new environment, the tests) the index is built
-- here and ready is set at once.
--
-- CREATE TRIGGER on chunks takes a lock that waits for running inserts (processing);
-- lock_timeout makes the migration fail cleanly instead of stalling processing.

SET search_path = ci, public;
SET LOCAL lock_timeout = '30s';

CREATE TABLE IF NOT EXISTS chunk_terms (
    chunk_id         uuid PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    communication_id uuid NOT NULL,
    version_id       uuid NOT NULL,
    is_doc           boolean NOT NULL,      -- chunk_kind = 'attachment_text'
    occurred_at      timestamptz NOT NULL,  -- the email's date, for score_limit
    tsv              tsvector NOT NULL
);
ALTER TABLE chunk_terms ALTER COLUMN tsv SET STORAGE MAIN;
ALTER TABLE chunk_terms SET (toast_tuple_target = 8160);

CREATE TABLE IF NOT EXISTS chunk_terms_state (
    only_row       boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    ready          boolean NOT NULL DEFAULT false,
    score_limit    integer NOT NULL DEFAULT 20000 CHECK (score_limit > 0),
    copied_through bigint NOT NULL DEFAULT -1,  -- last heap page of chunks copied
    ready_at       timestamptz
);
INSERT INTO chunk_terms_state DEFAULT VALUES ON CONFLICT DO NOTHING;

-- New chunks (processing inserts them in batches).
CREATE OR REPLACE FUNCTION chunk_terms_add() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ci, public, pg_temp
AS $$
BEGIN
    INSERT INTO chunk_terms (chunk_id, communication_id, version_id, is_doc, occurred_at, tsv)
    SELECT n.id, n.communication_id, n.version_id, n.chunk_kind = 'attachment_text',
           c.occurred_at, n.tsv
    FROM new_chunks n JOIN communications c ON c.id = n.communication_id
    ON CONFLICT (chunk_id) DO NOTHING;
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION chunk_terms_add() FROM PUBLIC;

-- Chunks are not changed after they are written today; this keeps the copy right
-- if that ever changes.
CREATE OR REPLACE FUNCTION chunk_terms_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ci, public, pg_temp
AS $$
BEGIN
    UPDATE chunk_terms ct
    SET tsv = n.tsv, is_doc = n.chunk_kind = 'attachment_text'
    FROM new_chunks n WHERE ct.chunk_id = n.id;
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION chunk_terms_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS chunks_terms_add ON chunks;
CREATE TRIGGER chunks_terms_add AFTER INSERT ON chunks
    REFERENCING NEW TABLE AS new_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION chunk_terms_add();
DROP TRIGGER IF EXISTS chunks_terms_change ON chunks;
CREATE TRIGGER chunks_terms_change AFTER UPDATE ON chunks
    REFERENCING NEW TABLE AS new_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION chunk_terms_change();

-- No chunks yet: nothing to copy, so build the index and switch over now.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM chunks) THEN
        CREATE INDEX IF NOT EXISTS chunk_terms_tsv ON chunk_terms USING gin (tsv);
        UPDATE chunk_terms_state SET ready = true, ready_at = now();
    END IF;
END
$$;

-- The k best keyword matches (ts_rank_cd), then the per-chunk check. Same result
-- columns and rules as 0031.
CREATE OR REPLACE FUNCTION published_lexical(q text, k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               rank real, scanned bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
DECLARE
    st chunk_terms_state;
BEGIN
    SELECT * INTO st FROM chunk_terms_state;
    IF st.ready THEN
        RETURN QUERY
        SELECT n.id, pc.id, pc.corpus_id, pc.occurred_at, n.rank, n.scanned
        FROM (SELECT t.*, count(*) OVER () AS scanned
              FROM (SELECT m.chunk_id AS id, m.communication_id AS cid, m.version_id,
                           ts_rank_cd(m.tsv, m.tq) AS rank
                    FROM (SELECT ct.*, tq FROM chunk_terms ct,
                                 websearch_to_tsquery('english', q) tq
                          WHERE ct.tsv @@ tq AND (docs IS NULL OR ct.is_doc = docs)
                          ORDER BY ct.occurred_at DESC, ct.chunk_id
                          LIMIT st.score_limit) m
                    ORDER BY rank DESC, m.chunk_id
                    LIMIT least(greatest(k, 1), 400)) t) n
        JOIN LATERAL (SELECT p.id, p.corpus_id, p.occurred_at FROM published_communications p
                      WHERE p.id = n.cid AND p.active_version_id = n.version_id
                      OFFSET 0) pc ON true;
        RETURN;
    END IF;
    RETURN QUERY
    SELECT n.id, pc.id, pc.corpus_id, pc.occurred_at, n.rank, n.scanned
    FROM (SELECT t.*, count(*) OVER () AS scanned
          FROM (SELECT c.id, c.communication_id AS cid, c.version_id,
                       ts_rank_cd(c.tsv, tq) AS rank
                FROM chunks c, websearch_to_tsquery('english', q) tq
                WHERE c.tsv @@ tq
                  AND (docs IS NULL OR (c.chunk_kind = 'attachment_text') = docs)
                ORDER BY rank DESC, c.id LIMIT least(greatest(k, 1), 400)) t) n
    JOIN LATERAL (SELECT p.id, p.corpus_id, p.occurred_at FROM published_communications p
                  WHERE p.id = n.cid AND p.active_version_id = n.version_id
                  OFFSET 0) pc ON true;
END
$$;

REVOKE ALL ON FUNCTION published_lexical(text, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_lexical(text, integer, boolean) TO ci_search;

-- The k best keyword matches among the active chunks of the emails in `ids`.
CREATE OR REPLACE FUNCTION published_lexical_in(
    q text, ids uuid[], k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, rank real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
DECLARE
    st chunk_terms_state;
BEGIN
    SELECT * INTO st FROM chunk_terms_state;
    IF st.ready THEN
        RETURN QUERY
        SELECT n.id, n.rank
        FROM (SELECT m.id, m.cid, m.version_id, ts_rank_cd(m.tsv, m.tq) AS rank
              FROM (SELECT ct.chunk_id AS id, ct.communication_id AS cid, ct.version_id,
                           ct.tsv, tq
                    FROM unnest(ids) u(id)
                    JOIN communications c ON c.id = u.id
                    -- Through chunks_version, so a few emails cost a few lookups; for
                    -- many emails the planner can start from the GIN index instead.
                    JOIN chunks ch ON ch.version_id = c.active_version_id
                    JOIN chunk_terms ct ON ct.chunk_id = ch.id,
                         websearch_to_tsquery('english', q) tq
                    WHERE ct.tsv @@ tq AND (docs IS NULL OR ct.is_doc = docs)
                    ORDER BY ct.occurred_at DESC, ct.chunk_id
                    LIMIT st.score_limit) m
              ORDER BY rank DESC, m.id LIMIT least(greatest(k, 1), 400)) n
        JOIN LATERAL (SELECT 1 FROM published_communications p
                      WHERE p.id = n.cid AND p.active_version_id = n.version_id
                      OFFSET 0) pc ON true
        ORDER BY n.rank DESC, n.id;
        RETURN;
    END IF;
    RETURN QUERY
    SELECT n.id, n.rank
    FROM (SELECT x.* FROM (
              SELECT ch.id, ch.communication_id AS cid, ch.version_id,
                     ts_rank_cd(ch.tsv, tq) AS rank
              FROM unnest(ids) u(id)
              JOIN communications c ON c.id = u.id
              JOIN chunks ch ON ch.version_id = c.active_version_id,
                   websearch_to_tsquery('english', q) tq
              WHERE ch.tsv @@ tq
                AND (docs IS NULL OR (ch.chunk_kind = 'attachment_text') = docs)
              OFFSET 0) x
          ORDER BY x.rank DESC, x.id LIMIT least(greatest(k, 1), 400)) n
    JOIN LATERAL (SELECT 1 FROM published_communications p
                  WHERE p.id = n.cid AND p.active_version_id = n.version_id
                  OFFSET 0) pc ON true
    ORDER BY n.rank DESC, n.id;
END
$$;

REVOKE ALL ON FUNCTION published_lexical_in(text, uuid[], integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_lexical_in(text, uuid[], integer, boolean) TO ci_search;
