-- 0032_files.sql
-- The console's Files page (Matthew, 2026-11-18: "a file explorer that default
-- view is newest with ability to search, have a view that groups files into
-- folders by project, and company, etc.").
--
-- published_files(since, until, text_query) returns the attachments of published,
-- active emails dated in [since, until), one row per attachment per email copy,
-- with the email's columns the page shows and groups by: date, subject, mailbox,
-- sender, and the AI's property and entity names. Signature images and logos
-- (inline, or skipped by extraction) are left out. With text_query, only
-- attachments whose extracted text matches it (full text, as search does).
--
-- Like published_labelled (0021) and published_window (0031) it returns only
-- what ci_search can already read through published_communications and
-- published_email_attachments, set-wise; authorization and filters are applied
-- by the caller (ci_retrieval.search._AUTH), before anything is ranked or counted.
--
-- file_key identifies the file across emails and mailbox copies: its SHA-256
-- when the original was read, else the message and the file name.
-- attachment_id is the short id get_email lists (ci_retrieval.search._ATTACHMENT_ID).

SET search_path = ci, public;

CREATE OR REPLACE FUNCTION published_files(
    since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL,
    text_query text DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz, title text,
               mailbox_address text, sender text, internet_message_id text,
               properties text[], entities text[], file_key text, attachment_id text,
               name text, content_type text, size_bytes bigint, extraction_status text,
               sha256 text, original_stored boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
-- Planned for the dates given: a generic plan could not use the date index.
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    WITH att AS MATERIALIZED (
        SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.active_version_id,
               a.native_attachment_id, a.name, a.content_type, a.size_bytes,
               a.extraction_status, a.sha256, a.original_stored_at IS NOT NULL AS stored
        FROM published_communications pc
        JOIN email_attachments a ON a.version_id = pc.active_version_id
        WHERE (since IS NULL OR pc.occurred_at >= since)
          AND (until IS NULL OR pc.occurred_at < until)
          AND NOT a.is_inline AND a.extraction_status <> 'skipped'
          AND (text_query IS NULL OR EXISTS (
                SELECT 1 FROM chunks ch
                WHERE ch.version_id = a.version_id AND ch.chunk_kind = 'attachment_text'
                  AND ch.source_ref = a.native_attachment_id
                  AND ch.tsv @@ websearch_to_tsquery('english', text_query)))),
    runs AS MATERIALIZED (  -- the latest classification of each version
        SELECT DISTINCT ON (r.version_id) r.version_id, r.properties, r.entities
        FROM classification_runs r
        WHERE r.version_id IN (SELECT att.active_version_id FROM att)
        ORDER BY r.version_id, r.id DESC),
    snd AS (
        SELECT DISTINCT ON (cp.communication_id) cp.communication_id, p.email_normalized
        FROM communication_participants cp JOIN participants p ON p.id = cp.participant_id
        WHERE cp.role = 'from' AND cp.communication_id IN (SELECT att.id FROM att)
        ORDER BY cp.communication_id)
    SELECT att.id, att.corpus_id, att.occurred_at, att.title, mb.primary_smtp_address,
           snd.email_normalized, em.internet_message_id,
           coalesce(r.properties, '{}'), coalesce(r.entities, '{}'),
           coalesce(att.sha256,
                    coalesce(em.internet_message_id, att.id::text) || '/' || coalesce(att.name, '')),
           left(encode(sha256(convert_to(att.native_attachment_id, 'UTF8')), 'hex'), 16),
           att.name, att.content_type, att.size_bytes, att.extraction_status, att.sha256,
           att.stored
    FROM att
    JOIN email_messages em ON em.communication_id = att.id
    JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
    LEFT JOIN runs r ON r.version_id = att.active_version_id
    LEFT JOIN snd ON snd.communication_id = att.id;
END
$$;

REVOKE ALL ON FUNCTION published_files(timestamptz, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_files(timestamptz, timestamptz, text) TO ci_search;
