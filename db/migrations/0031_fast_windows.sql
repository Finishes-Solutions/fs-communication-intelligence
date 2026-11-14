-- 0031_fast_windows.sql
-- MCP tools timed out and console pages took seconds once the 2025 backfill was
-- stored. Measured 2026-11-13 on a synthetic copy (300,000 emails, 225,000
-- published, 300,000 chunks): email_stats 5 to 73 s (the Reports page hit the
-- 2-minute statement limit), search_email with any filter 5 to 15 s, lists with a
-- property or sender filter 3 to 4 s. Three causes, all in how the published_*
-- views are read:
--   * a filter on a label, property, entity, mailbox or sender was an EXISTS on a
--     view per email (about 25 microseconds each, for every email in scope), and
--     reports joined published_classifications, which builds the label lists of
--     every published email before anything else;
--   * joining a handful of chunks to published_chunks made the planner evaluate
--     published_communications for every email (1 to 2 s per search, twice);
--   * a filtered search nearly always fell back to the exact scan, which computed
--     the distance to every chunk after those per-email checks.
--
-- The functions below read the same emails set-wise. Each returns only published,
-- active emails and chunks, exactly what ci_search can already read through the
-- published_* views; authorization and filters are applied by the caller
-- (ci_retrieval.search._AUTH) before ranking, as with 0015 and 0021.
--   published_window(since, until, ids, conversation, in_mailbox, from_address,
--     property_like, entity_like): published_labelled (0021) driven by the date, a list of ids or a
--     conversation instead of a label, with the caller's filters applied early.
--   published_ids(since, until): the same emails with only what a date or mailbox
--     filter needs (about five times cheaper).
--   published_nearest / published_lexical: the chunks nearest to a question from
--     the vector index, and the best keyword matches, checked one by one.
--   published_nearest_in / published_lexical_in: the same ranking, exact, within
--     the emails the caller found the reader may see (the filtered search).
--   published_chunk_rows: the text of the chunks a search returns.
-- Until this migration is applied, the code keeps using the views (slow, same answers).

SET search_path = ci, public;

CREATE OR REPLACE FUNCTION published_window(
    since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL,
    ids uuid[] DEFAULT NULL, conversation text DEFAULT NULL,
    in_mailbox text DEFAULT NULL, from_address text DEFAULT NULL,
    property_like text DEFAULT NULL, entity_like text DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz, title text,
               source_url text, mailbox_address text, direction text,
               internet_message_id text, content_fingerprint text, sender text,
               disposition text, summary text, action_text text, action_for text[],
               due_text text, risk_text text, properties text[], entities text[],
               yes_labels text[], uncertain_labels text[], attachments bigint,
               risk_impact text, risk_likelihood text, risk_reason text,
               conversation_id text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
-- Planned for the values given: a generic plan could not use the date index, the
-- id list, the conversation index or the sender's index.
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    IF conversation IS NOT NULL THEN
        -- The conversation's emails by id (within `ids` when both are given).
        ids := ARRAY(SELECT em.communication_id FROM email_messages em
                     WHERE em.conversation_id = conversation
                       AND (ids IS NULL OR em.communication_id = ANY(ids)));
    END IF;
    -- Every column is what the published_* views show for the same email, as in
    -- published_labelled; an email without a classification has NULLs and empty
    -- lists there, as a LEFT JOIN to published_classifications would give.
    -- The optional filters are the caller's own (ci_retrieval.search._FILTERS:
    -- mailbox address, a 'from' address, text within a property or entity name,
    -- already escaped for LIKE); they are applied here, before the label lists,
    -- attachment counts and senders are built, so a narrow filter over a long
    -- period stays cheap. They only narrow.
    RETURN QUERY
    WITH dated AS MATERIALIZED (
        SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.source_url,
               pc.active_version_id, mb.primary_smtp_address, em.direction,
               em.internet_message_id, em.content_fingerprint, em.conversation_id
        FROM published_communications pc
        JOIN email_messages em ON em.communication_id = pc.id
        JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
        WHERE (since IS NULL OR pc.occurred_at >= since)
          AND (until IS NULL OR pc.occurred_at < until)
          AND (ids IS NULL OR pc.id = ANY(ids))
          AND (in_mailbox IS NULL OR mb.primary_smtp_address = in_mailbox)
          AND (from_address IS NULL OR pc.id IN (
                SELECT cp.communication_id
                FROM participants p
                JOIN communication_participants cp ON cp.participant_id = p.id
                WHERE p.email_normalized = from_address AND cp.role = 'from'))),
    runs AS MATERIALIZED (  -- the latest run of each version
        SELECT DISTINCT ON (r.version_id) r.id, r.version_id, r.disposition, r.summary,
               r.action_text, r.action_for, r.due_text, r.risk_text, r.properties, r.entities
        FROM classification_runs r
        WHERE r.version_id IN (SELECT dated.active_version_id FROM dated)
        ORDER BY r.version_id, r.id DESC),
    pub AS MATERIALIZED (
        SELECT d.*, r.id AS run_id, r.disposition, r.summary, r.action_text, r.action_for,
               r.due_text, r.risk_text, r.properties, r.entities
        FROM dated d
        LEFT JOIN runs r ON r.version_id = d.active_version_id
        WHERE (property_like IS NULL OR EXISTS (
                SELECT 1 FROM unnest(r.properties) AS x(name)
                WHERE x.name ILIKE '%' || property_like || '%'))
          AND (entity_like IS NULL OR EXISTS (
                SELECT 1 FROM unnest(r.entities) AS x(name)
                WHERE x.name ILIKE '%' || entity_like || '%'))),
    lab AS (
        SELECT l.run_id,
               array_agg(l.label ORDER BY l.label) FILTER (WHERE l.decision = 'yes') AS yes,
               array_agg(l.label ORDER BY l.label)
                   FILTER (WHERE l.decision IN ('review', 'unresolved')) AS uncertain
        FROM classification_labels l
        WHERE l.decision <> 'no' AND l.run_id IN (SELECT pub.run_id FROM pub)
        GROUP BY l.run_id),
    att AS (
        SELECT a.version_id, count(*) AS n
        FROM email_attachments a
        WHERE a.extraction_status <> 'skipped'
          AND a.version_id IN (SELECT pub.active_version_id FROM pub)
        GROUP BY a.version_id),
    snd AS (
        SELECT DISTINCT ON (cp.communication_id) cp.communication_id, p.email_normalized
        FROM communication_participants cp JOIN participants p ON p.id = cp.participant_id
        WHERE cp.role = 'from' AND cp.communication_id IN (SELECT pub.id FROM pub)
        ORDER BY cp.communication_id)
    SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.source_url,
           pc.primary_smtp_address, pc.direction, pc.internet_message_id,
           pc.content_fingerprint, snd.email_normalized,
           pc.disposition, pc.summary, pc.action_text, pc.action_for, pc.due_text,
           pc.risk_text, coalesce(pc.properties, '{}'), coalesce(pc.entities, '{}'),
           coalesce(lab.yes, '{}'), coalesce(lab.uncertain, '{}'), coalesce(att.n, 0),
           rr.impact, rr.likelihood, rr.reason, pc.conversation_id
    FROM pub pc
    LEFT JOIN lab ON lab.run_id = pc.run_id
    LEFT JOIN att ON att.version_id = pc.active_version_id
    LEFT JOIN snd ON snd.communication_id = pc.id
    LEFT JOIN risk_ratings rr ON rr.version_id = pc.active_version_id;
END
$$;

REVOKE ALL ON FUNCTION published_window(
    timestamptz, timestamptz, uuid[], text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_window(
    timestamptz, timestamptz, uuid[], text, text, text, text, text) TO ci_search;

CREATE OR REPLACE FUNCTION published_ids(
    since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               mailbox_address text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    SELECT pc.id, pc.corpus_id, pc.occurred_at, mb.primary_smtp_address
    FROM published_communications pc
    JOIN email_messages em ON em.communication_id = pc.id
    JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
    WHERE (since IS NULL OR pc.occurred_at >= since)
      AND (until IS NULL OR pc.occurred_at < until);
END
$$;

REVOKE ALL ON FUNCTION published_ids(timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_ids(timestamptz, timestamptz) TO ci_search;

-- The chunk functions check "published" one chunk at a time (the LATERAL ... OFFSET 0
-- keeps it a per-chunk lookup): joined as a set, the planner evaluated
-- published_communications for every email to keep a few hundred chunks.
-- `docs`: NULL = any text, true = attachment text only, false = message text only.

-- nearest_published_chunks (0015) with the per-chunk check and the `docs` choice.
-- `scanned` is how many chunks the index gave (published or not): below k, every
-- chunk was seen. k is capped at 400 as in 0015.
CREATE OR REPLACE FUNCTION published_nearest(qv vector, k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               dist double precision, scanned bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET enable_seqscan = off
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    -- Set here, not in SET clauses: see 0015.
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);
    PERFORM set_config('hnsw.ef_search', '200', true);
    RETURN QUERY
    SELECT n.id, pc.id, pc.corpus_id, pc.occurred_at, n.dist, n.scanned
    FROM (SELECT ch.*, count(*) OVER () AS scanned
          FROM (SELECT c.id, c.communication_id AS cid, c.version_id,
                       c.embedding <=> qv AS dist
                FROM chunks c
                WHERE docs IS NULL OR (c.chunk_kind = 'attachment_text') = docs
                ORDER BY c.embedding <=> qv LIMIT least(greatest(k, 1), 400)) ch) n
    JOIN LATERAL (SELECT p.id, p.corpus_id, p.occurred_at FROM published_communications p
                  WHERE p.id = n.cid AND p.active_version_id = n.version_id
                  OFFSET 0) pc ON true;
END
$$;

REVOKE ALL ON FUNCTION published_nearest(vector, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_nearest(vector, integer, boolean) TO ci_search;

-- The k best keyword matches (ts_rank_cd), then the per-chunk check.
CREATE OR REPLACE FUNCTION published_lexical(q text, k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               rank real, scanned bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
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

-- The k chunks nearest to the question among the active chunks of the emails in
-- `ids`, by exact distance (the inner OFFSET 0 keeps the vector index out: with a
-- filter it can return too few). Only published chunks come back.
CREATE OR REPLACE FUNCTION published_nearest_in(
    qv vector, ids uuid[], k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, dist double precision)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    SELECT n.id, n.dist
    FROM (SELECT x.* FROM (
              SELECT ch.id, ch.communication_id AS cid, ch.version_id,
                     ch.embedding <=> qv AS dist
              FROM unnest(ids) u(id)
              JOIN communications c ON c.id = u.id
              JOIN chunks ch ON ch.version_id = c.active_version_id
              WHERE docs IS NULL OR (ch.chunk_kind = 'attachment_text') = docs
              OFFSET 0) x
          ORDER BY x.dist, x.id LIMIT least(greatest(k, 1), 400)) n
    JOIN LATERAL (SELECT 1 FROM published_communications p
                  WHERE p.id = n.cid AND p.active_version_id = n.version_id
                  OFFSET 0) pc ON true
    ORDER BY n.dist, n.id;
END
$$;

REVOKE ALL ON FUNCTION published_nearest_in(vector, uuid[], integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_nearest_in(vector, uuid[], integer, boolean) TO ci_search;

-- The k best keyword matches among the active chunks of the emails in `ids`.
CREATE OR REPLACE FUNCTION published_lexical_in(
    q text, ids uuid[], k integer, docs boolean DEFAULT NULL)
RETURNS TABLE (id uuid, rank real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
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

-- The chunks a search shows, as published_chunks and published_email_attachments
-- give them (text, kind, the attachment's name).
CREATE OR REPLACE FUNCTION published_chunk_rows(ids uuid[])
RETURNS TABLE (id uuid, communication_id uuid, chunk_kind text, display_text text,
               source_ref text, attachment_name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    SELECT ch.id, ch.communication_id, ch.chunk_kind, ch.display_text, ch.source_ref, a.name
    FROM chunks ch
    JOIN LATERAL (SELECT 1 FROM published_communications p
                  WHERE p.id = ch.communication_id AND p.active_version_id = ch.version_id
                  OFFSET 0) pc ON true
    LEFT JOIN email_attachments a
      ON a.version_id = ch.version_id AND a.native_attachment_id = ch.source_ref
    WHERE ch.id = ANY(ids);
END
$$;

REVOKE ALL ON FUNCTION published_chunk_rows(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_chunk_rows(uuid[]) TO ci_search;
