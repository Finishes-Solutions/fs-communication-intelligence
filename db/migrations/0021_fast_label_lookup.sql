-- 0021_fast_label_lookup.sql
-- The console's Risk page took tens of seconds and timed out at about 100,000
-- emails (measured 2026-10-29 on a synthetic copy). Two causes:
--   * published_classifications builds the yes/uncertain label arrays of every
--     published email before any filter applies, so "emails labelled X" cost
--     about 9 s whatever X is;
--   * the planner, unable to estimate that filter, chose nested loops that
--     compared tens of millions of rows.
-- published_labelled(labels, since, until) goes the other way: from the labels index to the
-- runs that say yes, then keeps the latest run of each published email. It
-- returns only published, active emails, exactly as published_classifications
-- would show them, so it gives ci_search nothing it cannot already read.
-- Authorization and filters are applied by the caller (ci_retrieval.reports),
-- as with nearest_published_chunks (0015).

SET search_path = ci, public;

CREATE INDEX IF NOT EXISTS classification_labels_yes
    ON classification_labels (label, run_id) WHERE decision = 'yes';

CREATE OR REPLACE FUNCTION published_labelled(
    labels text[], since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz, title text,
               source_url text, mailbox_address text, direction text,
               internet_message_id text, content_fingerprint text, sender text,
               disposition text, summary text, action_text text, action_for text[],
               due_text text, risk_text text, properties text[], entities text[],
               yes_labels text[], uncertain_labels text[], attachments bigint,
               risk_impact text, risk_likelihood text, risk_reason text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $$
    -- Every column is what the published_* views show for the same email
    -- (published_communications, published_email_messages, published_participants,
    -- published_classifications, published_email_attachments, published_risk_ratings),
    -- computed set-wise for the matching emails only.
    WITH hit AS (
        SELECT DISTINCT l.run_id FROM classification_labels l
        WHERE l.label = ANY(labels) AND l.decision = 'yes'),
    runs AS (  -- the latest run of each version, as published_classifications shows it
        SELECT r.* FROM hit JOIN classification_runs r ON r.id = hit.run_id
        WHERE NOT EXISTS (SELECT 1 FROM classification_runs later
                          WHERE later.version_id = r.version_id AND later.id > r.id)),
    pub AS (  -- optionally only emails dated in [since, until)
        SELECT pc.*, runs.id AS run_id
        FROM runs JOIN published_communications pc ON pc.active_version_id = runs.version_id
        WHERE (since IS NULL OR pc.occurred_at >= since)
          AND (until IS NULL OR pc.occurred_at < until)),
    att AS (
        SELECT a.version_id, count(*) AS n
        FROM email_attachments a JOIN pub ON pub.active_version_id = a.version_id
        WHERE a.extraction_status <> 'skipped' GROUP BY a.version_id),
    snd AS (
        SELECT DISTINCT ON (cp.communication_id) cp.communication_id, p.email_normalized
        FROM communication_participants cp JOIN participants p ON p.id = cp.participant_id
        WHERE cp.role = 'from' AND cp.communication_id IN (SELECT id FROM pub)
        ORDER BY cp.communication_id)
    SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.title, pc.source_url,
           mb.primary_smtp_address, em.direction, em.internet_message_id,
           em.content_fingerprint, snd.email_normalized,
           r.disposition, r.summary, r.action_text, r.action_for, r.due_text, r.risk_text,
           r.properties, r.entities,
           coalesce(lab.yes, '{}'), coalesce(lab.uncertain, '{}'), coalesce(att.n, 0),
           rr.impact, rr.likelihood, rr.reason
    FROM pub pc
    JOIN runs r ON r.id = pc.run_id
    JOIN email_messages em ON em.communication_id = pc.id
    JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
    -- Both label lists in one pass over the run's labels (primary key index).
    LEFT JOIN LATERAL (
        SELECT array_agg(l.label ORDER BY l.label) FILTER (WHERE l.decision = 'yes') AS yes,
               array_agg(l.label ORDER BY l.label)
                   FILTER (WHERE l.decision IN ('review', 'unresolved')) AS uncertain
        FROM classification_labels l WHERE l.run_id = r.id) lab ON true
    LEFT JOIN att ON att.version_id = r.version_id
    LEFT JOIN snd ON snd.communication_id = pc.id
    LEFT JOIN risk_ratings rr ON rr.version_id = r.version_id
$$;

REVOKE ALL ON FUNCTION published_labelled(text[], timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_labelled(text[], timestamptz, timestamptz) TO ci_search;
