-- 0015_fast_vector_search.sql
-- Semantic search used the HNSW index only on paper: the search query reads
-- published_chunks, a view joined to communications, scopes, exclusions and
-- tombstones, and with any join PostgreSQL chose to compute the distance to
-- every published chunk (measured 2026-10-28 on 50,000 synthetic chunks: 750 ms,
-- growing with the mail indexed). This function asks the index alone for the
-- chunks nearest to the query, then keeps only those visible in
-- published_chunks, so it returns nothing the search role cannot already read.
-- It returns the columns the search query's grants and filters need
-- (communication, corpus, date, all read from published_chunks), so they apply
-- to the candidates before ranking without joining the view again; the query
-- falls back to the exact scan when too few candidates remain (ci_retrieval.search).
--
-- `scanned` is how many nearest chunks were taken from the index (published or
-- not); when it is below k, every chunk was seen. k is capped at 400: search
-- asks for 200, and much larger iterative index scans can take minutes.

SET search_path = ci, public;

CREATE OR REPLACE FUNCTION nearest_published_chunks(qv vector, k integer)
RETURNS TABLE (id uuid, communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               dist double precision, scanned bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
-- The index is the only other path for ORDER BY distance; pgvector's cost
-- estimate otherwise flips to a full scan for some k / ef_search values.
SET enable_seqscan = off
AS $$
BEGIN
    -- pgvector's settings are set here, not in the SET clauses above: a
    -- function's SET clause may name an extension setting only as superuser,
    -- which Azure does not grant. They last until the caller's transaction ends.
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);
    PERFORM set_config('hnsw.ef_search', '200', true);
    RETURN QUERY
    SELECT n.id, pc.communication_id, pc.corpus_id, pc.occurred_at, n.dist, n.scanned
    FROM (SELECT ch.id, ch.dist, count(*) OVER () AS scanned
          FROM (SELECT c.id, c.embedding <=> qv AS dist FROM chunks c
                ORDER BY c.embedding <=> qv LIMIT least(greatest(k, 1), 400)) ch) n
    JOIN published_chunks pc ON pc.id = n.id;
END
$$;

REVOKE ALL ON FUNCTION nearest_published_chunks(vector, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION nearest_published_chunks(vector, integer) TO ci_search;
