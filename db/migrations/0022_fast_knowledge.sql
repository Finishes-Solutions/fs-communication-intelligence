-- 0022_fast_knowledge.sql
-- The console's Knowledge pages took 4-11 s on the dev server and 20-80 s on a
-- synthetic copy with 100,000 emails (measured 2026-10-29). Every read went
-- through published_kg_mentions, which cannot use an index for "mentions of
-- entry X" (an entry's id is coalesce(merged_into, id)), so each of the 6-10
-- queries behind one page scanned every mention of every published email.
--
-- published_kg_mentions_of(roots, near, kinds, since) returns the same rows as
-- published_kg_mentions joined to published_email_messages (plus the message
-- identity that counts copies in several mailboxes once), but starts from the
-- indexes:
--   roots  the mentions of these entries (their own rows and those merged into them);
--   near   every mention in the messages that mention this entry;
--   both NULL: every mention, as before.
-- It returns only published, active emails and visible entries, exactly as the
-- views show them, so it gives ci_search nothing it cannot already read.
-- Authorization is applied by the caller (ci_retrieval.knowledge), as with
-- nearest_published_chunks (0015) and published_labelled (0021).

SET search_path = ci, public;

-- Mentions and facts reach their email through the version id.
CREATE INDEX IF NOT EXISTS communications_active_version ON communications (active_version_id);
CREATE INDEX IF NOT EXISTS kg_entities_root
    ON kg_entities ((coalesce(merged_into, id)));

-- Columns are those of published_kg_mentions (0016, 0020) plus `msg`, the
-- Internet Message-ID (else the communication id), as published_email_messages
-- gives it. With `roots` or `near`, the candidate emails are found through the
-- indexes first and then checked against published_communications as a
-- constant list: joined directly, the planner reads every published email
-- even for an entry named in five of them.
CREATE OR REPLACE FUNCTION published_kg_mentions_of(
    roots bigint[] DEFAULT NULL, near bigint DEFAULT NULL,
    kinds text[] DEFAULT NULL, since timestamptz DEFAULT NULL)
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               entity_id bigint, kind text, key text, name_override text,
               detail_override text, written text, detail text, msg text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $fn$
DECLARE
    ids uuid[];
BEGIN
    IF roots IS NULL AND near IS NULL THEN
        RETURN QUERY EXECUTE $q$
            SELECT pc.id, pc.corpus_id, pc.occurred_at, v.entity_id, r.kind, r.key,
                   r.name_override, r.detail_override, m.written, m.detail,
                   coalesce(em.internet_message_id, pc.id::text)
            FROM kg_mentions m
            JOIN kg_visible_entities v ON v.id = m.entity_id
            JOIN kg_entities r ON r.id = v.entity_id
            JOIN published_communications pc ON pc.active_version_id = m.version_id
            JOIN email_messages em ON em.communication_id = pc.id
            JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
            WHERE ($1::text[] IS NULL OR r.kind = ANY($1))
              AND ($2::timestamptz IS NULL OR pc.occurred_at >= $2)$q$ USING kinds, since;
        RETURN;
    END IF;
    -- The emails whose current version names the entries (or `near`).
    ids := ARRAY(
        SELECT DISTINCT c.id
        FROM kg_visible_entities v
        JOIN kg_mentions m ON m.entity_id = v.id
        JOIN communications c ON c.active_version_id = m.version_id
        WHERE v.entity_id = ANY(coalesce(roots, ARRAY[near])));
    -- Up to 20,000 are looked up one by one; more are hash-joined with all
    -- published emails, which is cheaper than that many lookups.
    RETURN QUERY EXECUTE format($q$
        WITH pub AS (
            SELECT pc.id, pc.corpus_id, pc.occurred_at, pc.active_version_id,
                   coalesce(em.internet_message_id, pc.id::text) AS msg
            FROM %s
            JOIN email_messages em ON em.communication_id = pc.id
            JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
            WHERE ($4::timestamptz IS NULL OR pc.occurred_at >= $4) %s)
        SELECT pub.id, pub.corpus_id, pub.occurred_at, v.entity_id, r.kind, r.key,
               r.name_override, r.detail_override, m.written, m.detail, pub.msg
        FROM pub
        JOIN kg_mentions m ON m.version_id = pub.active_version_id
        JOIN kg_visible_entities v ON v.id = m.entity_id
        JOIN kg_entities r ON r.id = v.entity_id
        WHERE ($2::bigint[] IS NULL OR v.entity_id = ANY($2))
          AND ($3::text[] IS NULL OR r.kind = ANY($3))$q$,
        CASE WHEN cardinality(ids) <= 20000 THEN 'published_communications pc'
             ELSE 'unnest($1) AS ids(id) JOIN published_communications pc ON pc.id = ids.id' END,
        CASE WHEN cardinality(ids) <= 20000 THEN 'AND pc.id = ANY($1)' ELSE '' END)
        USING ids, roots, kinds, since;
END
$fn$;

-- The facts that name any of `roots` as subject or object, with the columns of
-- published_kg_relations plus `msg` and the sender address
-- (published_participants, role 'from'), for counting support.
CREATE OR REPLACE FUNCTION published_kg_relations_of(roots bigint[])
RETURNS TABLE (communication_id uuid, corpus_id uuid, occurred_at timestamptz,
               subject_id bigint, relation text, role text, object_id bigint,
               evidence text, msg text, sender text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $fn$
DECLARE
    ids uuid[];
BEGIN
    ids := ARRAY(
        SELECT DISTINCT c.id
        FROM kg_visible_entities v
        JOIN kg_relations k ON k.subject_id = v.id OR k.object_id = v.id
        JOIN communications c ON c.active_version_id = k.version_id
        WHERE v.entity_id = ANY(roots));
    RETURN QUERY EXECUTE $q$
        SELECT pc.id, pc.corpus_id, pc.occurred_at, s.entity_id, k.relation, k.role,
               o.entity_id, k.evidence, coalesce(em.internet_message_id, pc.id::text),
               (SELECT p.email_normalized FROM communication_participants cp
                JOIN participants p ON p.id = cp.participant_id
                WHERE cp.communication_id = pc.id AND cp.role = 'from' LIMIT 1)
        FROM published_communications pc
        JOIN email_messages em ON em.communication_id = pc.id
        JOIN email_mailboxes mb ON mb.scope_id = em.mailbox_scope_id
        JOIN kg_relations k ON k.version_id = pc.active_version_id
        JOIN kg_visible_entities s ON s.id = k.subject_id
        JOIN kg_visible_entities o ON o.id = k.object_id
        WHERE pc.id = ANY($1)
          AND s.entity_id <> o.entity_id
          AND (s.entity_id = ANY($2) OR o.entity_id = ANY($2))$q$ USING ids, roots;
END
$fn$;

-- Which of `labels` each of the emails `ids` has as yes, from its latest
-- classification run, for published, active emails only (what
-- published_classifications.yes_labels shows) without building every email's
-- label lists first.
CREATE OR REPLACE FUNCTION published_label_hits(ids uuid[], labels text[])
RETURNS TABLE (communication_id uuid, label text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ci, public
AS $fn$
BEGIN
    RETURN QUERY EXECUTE $q$
        SELECT pc.id, l.label
        FROM published_communications pc
        JOIN LATERAL (SELECT r.id FROM classification_runs r
                      WHERE r.version_id = pc.active_version_id
                      ORDER BY r.id DESC LIMIT 1) r ON true
        JOIN classification_labels l ON l.run_id = r.id
        WHERE pc.id = ANY($1) AND l.label = ANY($2) AND l.decision = 'yes'$q$
        USING ids, labels;
END
$fn$;

REVOKE ALL ON FUNCTION published_kg_mentions_of(bigint[], bigint, text[], timestamptz),
    published_kg_relations_of(bigint[]), published_label_hits(uuid[], text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION published_kg_mentions_of(bigint[], bigint, text[], timestamptz),
    published_kg_relations_of(bigint[]), published_label_hits(uuid[], text[]) TO ci_search;
