-- 0037_published_page.sql
-- A list page costs about a page (performance round, 2026-11-18). Since 0031 a
-- list read every published email of its period through published_window, then
-- kept the newest 500 the reader may see: "everything" read every email, for page
-- one as for page ten.
--
-- published_page(since, until, in_mailbox, from_address, row_limit) picks the ids
-- of the newest row_limit published emails (at most 20,000) that match those four
-- filters, ordered occurred_at DESC, id, and returns their published_window rows
-- (the same columns, unordered). The newest 200 cost about 200 emails' work.
-- Like published_window it returns only published, active emails, exactly what
-- ci_search can already read through the published_* views; authorization and the
-- other filters are applied by the caller (ci_retrieval.search._AUTH, as a WHERE
-- in the same statement) before anything leaves the database. When the newest
-- rows do not fill the reader's page, the caller asks for more and then falls
-- back to the 0031 read (ci_retrieval.search._listing_rows_paged).
--
-- The ids are picked with the same joins published_window uses (an email with its
-- message and mailbox), so every id picked comes back: a caller that gets fewer
-- than row_limit rows knows it saw every email of the period.
-- docs/DATABASE.md §7 "A list page", docs/DEPLOY_AZURE.md "Search speed".

SET search_path = ci, public;

CREATE OR REPLACE FUNCTION published_page(
    since timestamptz, until timestamptz, in_mailbox text, from_address text,
    row_limit integer)
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
-- Planned for the values given, as published_window (0031).
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
DECLARE
    n integer := least(greatest(coalesce(row_limit, 1), 1), 20000);
    picked uuid[];
BEGIN
    IF in_mailbox IS NULL AND from_address IS NULL THEN
        -- Newest first straight from the date index, stopping after n.
        picked := ARRAY(
            SELECT pc.id
            FROM published_communications pc
            JOIN email_messages em ON em.communication_id = pc.id
            JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
            WHERE (since IS NULL OR pc.occurred_at >= since)
              AND (until IS NULL OR pc.occurred_at < until)
            ORDER BY pc.occurred_at DESC, pc.id
            LIMIT n);
    ELSE
        -- A mailbox or sender: its emails first (their own indexes), then the newest.
        picked := ARRAY(
            WITH matching AS MATERIALIZED (
                SELECT pc.id, pc.occurred_at
                FROM published_communications pc
                JOIN email_messages em ON em.communication_id = pc.id
                JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
                WHERE (since IS NULL OR pc.occurred_at >= since)
                  AND (until IS NULL OR pc.occurred_at < until)
                  AND (in_mailbox IS NULL OR mb.primary_smtp_address = in_mailbox)
                  AND (from_address IS NULL OR pc.id IN (
                        SELECT cp.communication_id
                        FROM participants p
                        JOIN communication_participants cp ON cp.participant_id = p.id
                        WHERE p.email_normalized = from_address AND cp.role = 'from')))
            SELECT m.id FROM matching m
            ORDER BY m.occurred_at DESC, m.id
            LIMIT n);
    END IF;
    RETURN QUERY SELECT w.* FROM published_window(NULL, NULL, picked) w;
END
$$;

REVOKE ALL ON FUNCTION published_page(timestamptz, timestamptz, text, text, integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_page(timestamptz, timestamptz, text, text, integer)
    TO ci_search;
